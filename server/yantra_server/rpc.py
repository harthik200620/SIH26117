"""RPC dispatcher and the event bus that streams run notifications to clients.

Handlers register with @rpc_method("name", ParamsModel); milestones add methods as their
subsystems land, so an unimplemented method is METHOD_NOT_FOUND, never a fake.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from yantra_server.protocol.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    RpcError,
    notification_frame,
)
from yantra_server.protocol.messages import Notification

if TYPE_CHECKING:
    from yantra_server.db.base import Database
    from yantra_server.state import AppState

log = logging.getLogger(__name__)

RING_SIZE = 20_000


@dataclass
class Connection:
    """One WebSocket client: an outbound queue the socket task drains."""

    id: str
    outbound: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(4096))
    session_ids: set[str] = field(default_factory=set)

    def try_send(self, frame: dict[str, Any]) -> None:
        try:
            self.outbound.put_nowait(frame)
        except asyncio.QueueFull:
            log.warning("connection %s outbound queue full; dropping frame", self.id)


class EventBus:
    """Per-run monotonic sequence numbers + replay ring, fan-out to live connections."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self._publish_lock = threading.Lock()
        self._connections: dict[str, Connection] = {}
        self._rings: dict[str, deque[dict[str, Any]]] = {}
        self._seqs: dict[str, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def attach(self, conn: Connection) -> None:
        self._connections[conn.id] = conn

    def detach(self, conn_id: str) -> None:
        self._connections.pop(conn_id, None)

    def publish(self, note: Notification) -> None:
        run_id = note.run_id or "_global"
        with self._publish_lock:
            seq = self.latest_seq(run_id) + 1
            note.seq = seq
            frame = notification_frame(type(note).method, note.model_dump(mode="json"))
            if self._db is not None and note.run_id:
                from yantra_server.db.models import RunEventRow

                with self._db.session() as session:
                    session.add(RunEventRow(run_id=run_id, seq=seq, frame=frame))
            self._seqs[run_id] = seq
            self._rings.setdefault(run_id, deque(maxlen=RING_SIZE)).append(frame)
        for conn in list(self._connections.values()):
            conn.try_send(frame)

    def latest_seq(self, run_id: str) -> int:
        if run_id not in self._seqs and self._db is not None:
            from sqlalchemy import func, select

            from yantra_server.db.models import RunEventRow

            with self._db.session() as session:
                self._seqs[run_id] = (
                    session.scalar(
                        select(func.max(RunEventRow.seq)).where(RunEventRow.run_id == run_id)
                    )
                    or 0
                )
        return self._seqs.get(run_id, 0)

    def publish_threadsafe(self, note: Notification) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self.publish, note)

    def replay(self, run_id: str, after_seq: int) -> list[dict[str, Any]]:
        if self._db is not None:
            from sqlalchemy import select

            from yantra_server.db.models import RunEventRow

            with self._db.session() as session:
                return list(
                    session.scalars(
                        select(RunEventRow.frame)
                        .where(RunEventRow.run_id == run_id, RunEventRow.seq > after_seq)
                        .order_by(RunEventRow.seq)
                        .limit(RING_SIZE)
                    )
                )
        ring = self._rings.get(run_id)
        if not ring:
            return []
        return [f for f in ring if int(f["params"].get("seq", 0)) > after_seq]


Handler = Callable[["AppState", Connection, Any], Awaitable[Any]]

_METHODS: dict[str, tuple[type[BaseModel], Handler]] = {}


def rpc_method(name: str, params_model: type[BaseModel]) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        _METHODS[name] = (params_model, fn)
        return fn

    return register


def registered_methods() -> list[str]:
    return sorted(_METHODS)


async def dispatch(state: AppState, conn: Connection, method: str, params: dict[str, Any]) -> Any:
    entry = _METHODS.get(method)
    if entry is None:
        raise RpcError(METHOD_NOT_FOUND, f"unknown method {method!r}")
    params_model, handler = entry
    try:
        parsed = params_model.model_validate(params)
    except ValidationError as exc:
        raise RpcError(INVALID_PARAMS, f"invalid params for {method}", data=exc.errors()) from exc
    try:
        result = await handler(state, conn, parsed)
    except RpcError:
        raise
    except Exception as exc:
        log.exception("handler %s failed", method)
        raise RpcError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}") from exc
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    return result
