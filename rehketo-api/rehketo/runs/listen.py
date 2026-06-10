from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from rehketo.config import get_settings
from rehketo.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_RECONNECT_DELAY_SECONDS = 1.0


def _raw_dsn() -> str:
    """psycopg wants a plain postgresql:// DSN, not SQLAlchemy's +psycopg form."""
    return get_settings().database_url.replace("+psycopg", "", 1)


async def listen(
    channel: str,
    on_payload: Callable[[str], None],
    *,
    ready: asyncio.Event | None = None,
) -> None:
    """LISTEN on `channel` forever, dispatching each payload to `on_payload`.

    Holds a dedicated autocommit connection (LISTEN pins a connection, so it
    must not come from the pool). Reconnects with a fixed delay on any
    connection failure — subscribers degrade to their re-poll interval while
    the listener is down, so data is never lost, only delayed. Runs until
    cancelled; intended as a long-lived asyncio.Task owned by app lifespan.
    `ready` is set after the first successful LISTEN and never cleared on
    reconnect — it is a startup gate, not a liveness signal.
    """
    while True:
        try:
            conn = await psycopg.AsyncConnection.connect(_raw_dsn(), autocommit=True)
            try:
                await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
                if ready is not None:
                    ready.set()
                async for notification in conn.notifies():
                    try:
                        on_payload(notification.payload)
                    except Exception:
                        # A bad payload must cost one notification, not the
                        # subscription — anyone with DB access can NOTIFY
                        # arbitrary bytes on the channel.
                        logger.exception("on_payload failed for channel %s", channel)
            finally:
                await conn.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LISTEN %s connection lost; reconnecting", channel)
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
