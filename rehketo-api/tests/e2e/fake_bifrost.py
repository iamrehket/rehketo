"""In-test fake of Bifrost's OpenAI-compatible chat-completions endpoint.

The api's agent (`rehketo/agent/llm.py`) builds `ChatOpenAI(
use_responses_api=False, streaming=True)` pointed at `BIFROST_BASE_URL`.
At runtime that means `POST /v1/chat/completions` with `stream=true`,
consumed as OpenAI Server-Sent Events: lines `data: {...}\\n\\n` and a
terminator `data: [DONE]\\n\\n`.

Title generation makes a SEPARATE non-streaming call (also chat/completions
but without `stream`); we serve the same content as a single JSON object
in that case.

A small admin route (`POST /__test__/mode`) lets individual e2e tests
switch profiles without restarting the server:

- ``default``   — three streamed chunks "Hello ", "world", "!"
- ``slow``      — ten chunks at 100 ms each (for cancel-mid-stream)
- ``title-fail`` — second call (title generation) returns 500
- ``marathon``  — forty chunks at 250 ms each (for live-resume/chaos)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

_PROFILES: dict[str, dict[str, Any]] = {
    "default": {
        "chunks": ("Hello ", "world", "!"),
        "delay_s": 0.0,
        "title_fail": False,
    },
    "slow": {
        "chunks": tuple("abcdefghij"),
        "delay_s": 0.1,
        "title_fail": False,
    },
    "title-fail": {
        "chunks": ("Hi", "!"),
        "delay_s": 0.0,
        "title_fail": True,
    },
    "marathon": {
        # 40 x 250 ms = 10 s stream: long enough for a second tab to open
        # mid-stream (cold SPA open ≈ 3-5 s on CI) and for chaos kills; short
        # enough for a 60 s per-spec timeout.
        "chunks": tuple(f"tok{i} " for i in range(40)),
        "delay_s": 0.25,
        "title_fail": False,
    },
}


def make_app() -> FastAPI:
    app = FastAPI(title="fake-bifrost")
    # Module-level mutable state is fine — single fake server per session.
    state: dict[str, Any] = {"profile": "default", "_call_count": 0}

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/__test__/mode")
    async def set_mode(req: Request) -> Any:
        body = await req.json()
        name = body.get("profile", "default")
        if name not in _PROFILES:
            return JSONResponse({"error": "unknown profile"}, status_code=400)
        state["profile"] = name
        state["_call_count"] = 0
        return {"profile": name}

    @app.post("/v1/chat/completions")
    async def chat(req: Request) -> Any:
        state["_call_count"] += 1
        profile = _PROFILES[state["profile"]]
        body = await req.json()
        model = body.get("model", "claude-sonnet-4-6")
        is_streaming = bool(body.get("stream", False))

        # title-fail profile: chat works, but the second (title-gen) call 500s.
        if profile["title_fail"] and state["_call_count"] > 1:
            return JSONResponse({"error": "title llm down"}, status_code=500)

        chunks: tuple[str, ...] = profile["chunks"]
        delay_s: float = profile["delay_s"]
        cid = f"chatcmpl-{uuid.uuid4()}"
        created = int(time.time())
        full_text = "".join(chunks)

        if not is_streaming:
            return JSONResponse(
                {
                    "id": cid,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": full_text,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

        async def gen() -> Any:
            # Role chunk first — real Bifrost / OpenAI emit this; LangChain
            # tolerates absence but we match production wire format.
            first = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(first)}\n\n"
            for chunk in chunks:
                if delay_s:
                    await asyncio.sleep(delay_s)
                payload = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(payload)}\n\n"
            done = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
