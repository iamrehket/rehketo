from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents import create_deep_agent
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from rehketo.agent.llm import build_chat_model
from rehketo.config import get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from langchain.agents.middleware import InterruptOnConfig
    from langchain_core.tools import BaseTool
    from langgraph.graph.state import CompiledStateGraph


def _checkpointer_dsn() -> str:
    raw = get_settings().database_url
    # LangGraph's checkpointer wants a plain postgresql:// DSN, not +psycopg.
    return raw.replace("postgresql+psycopg://", "postgresql://", 1)


async def build_agent(
    run_id: str,
    system_prompt: str,
    tools: Sequence[BaseTool] = (),
    interrupt_on: Mapping[str, InterruptOnConfig] | None = None,
) -> AsyncIterator[CompiledStateGraph]:  # type: ignore[type-arg]
    """Yield a deepagents graph bound to a postgres checkpointer.

    Scoped to thread_id=run_id. Tools and the per-tool approval config are
    assembled by the caller (rehketo.mcp.registry) so graph construction
    stays a pure function of its inputs. interrupt_on installs deepagents'
    HumanInTheLoopMiddleware: listed tools pause the graph for approval
    before executing; unlisted tools auto-approve.
    """
    dsn = _checkpointer_dsn()
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        agent: CompiledStateGraph = create_deep_agent(  # type: ignore[type-arg]
            tools=list(tools),
            system_prompt=system_prompt,
            model=build_chat_model(),
            checkpointer=saver,
            interrupt_on=dict(interrupt_on) if interrupt_on else None,
        )
        yield agent
