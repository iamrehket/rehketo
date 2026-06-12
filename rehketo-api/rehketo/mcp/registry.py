"""Per-run MCP client lifecycle: connect to each allowed server, list its
tools, adapt them, and build the per-tool HITL approval config from each
server's auto_approve flag. Connections are per-run (opened by run_agent,
closed via the caller's AsyncExitStack) — no shared state across requests or
processes, the property M1 established and the M4 worker split depends on."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from langchain.agents.middleware import InterruptOnConfig

from rehketo.auth.crypto import decrypt_token
from rehketo.core.logging import format_exc_for_log, get_logger
from rehketo.mcp.adapter import build_structured_tool

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AsyncExitStack

    from langchain_core.tools import StructuredTool

    from rehketo.db.models import McpServer
    from rehketo.runs.event_bus import RunEventBus

logger = get_logger(__name__)


async def _close_client(name: str, client: Client) -> None:  # type: ignore[type-arg]  # transport generic is irrelevant here
    # Same stance as connect: a tool server dying mid-run must not change
    # the run's outcome at cleanup time. close() (force-capable) rather than
    # __aexit__, which re-raises the dead session's exception.
    try:
        await client.close()
    except Exception as exc:
        logger.warning("mcp server %s close failed: %s", name, format_exc_for_log(exc))


# Providers constrain function-tool names (OpenAI-compatible contract).
# Validated on the COMBINED name because server slugs alone don't bound
# what an MCP server may call its tools.
_PROVIDER_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Servers connect sequentially on the chat critical path; cap each one so a
# hung TCP handshake or slow remote doesn't stall the entire run start.
_SERVER_CONNECT_TIMEOUT_S = 10.0


def _client_for(server: McpServer) -> Client:  # type: ignore[type-arg]  # transport generic is irrelevant here
    """Client construction seam — tests monkeypatch this to inject an
    in-memory FastMCP transport."""
    token = decrypt_token(server.auth_token_ct) if server.auth_token_ct else None
    return Client(StreamableHttpTransport(server.url, auth=token))


async def build_run_toolset(
    stack: AsyncExitStack,
    servers: Sequence[McpServer],
    *,
    run_id: str,
    bus: RunEventBus,
) -> tuple[list[StructuredTool], dict[str, InterruptOnConfig]]:
    """Connect to each allowed MCP server, list and adapt its tools.

    Returns the adapted tools plus the HITL interrupt config: every tool from
    an auto_approve=False server requires per-call user approval (approve/reject
    only — the M3.5 decision vocabulary); tools from trusted servers are absent,
    which the middleware auto-approves.
    """
    tools: list[StructuredTool] = []
    interrupt_on: dict[str, InterruptOnConfig] = {}
    for server in servers:
        try:
            client = _client_for(server)
            async with asyncio.timeout(_SERVER_CONNECT_TIMEOUT_S):
                # Deliberate: skip __aexit__ to use _close_client instead,
                # which swallows dead-session exceptions at cleanup time.
                await client.__aenter__()
                stack.push_async_callback(_close_client, server.name, client)
                mcp_tools = await client.list_tools()
        except Exception as exc:
            # A broken tool server must not take chat down: skip it, the
            # run proceeds with the remaining tools (spec: error handling).
            logger.warning(
                "mcp server %s unavailable, skipping: %s",
                server.name,
                format_exc_for_log(exc),
            )
            continue
        for t in mcp_tools:
            full_name = f"{server.name}__{t.name}"
            if not _PROVIDER_TOOL_NAME.fullmatch(full_name):
                logger.warning(
                    "skipping tool with provider-invalid name %s from server %s",
                    full_name,
                    server.name,
                )
                continue
            tools.append(
                build_structured_tool(
                    server_name=server.name,
                    tool=t,
                    client=client,
                    run_id=run_id,
                    bus=bus,
                )
            )
            if not server.auto_approve:
                interrupt_on[full_name] = InterruptOnConfig(
                    allowed_decisions=["approve", "reject"]
                )
    return tools, interrupt_on
