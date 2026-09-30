"""Seal monitor (SPEC §14.4): aggregates guard events, layer states, counters."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from yantra_server.db.base import Database
from yantra_server.db.models import SealEventRow
from yantra_server.protocol.messages import SealEventNote
from yantra_server.rpc import EventBus

if TYPE_CHECKING:
    from yantra_server.config import YantraConfig

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 3.0


class SealMonitor:
    def __init__(self, config: YantraConfig, db: Database, bus: EventBus, assets_dir: Path) -> None:
        self.config = config
        self.db = db
        self.bus = bus
        self.assets_dir = assets_dir
        self.events_file = config.paths.data_dir / "seal_events.jsonl"
        self._task: asyncio.Task[None] | None = None
        self._offset = 0

    # ------------------------------------------------------------- ingestion

    def record_event(self, event: dict[str, Any]) -> None:
        """Reporter callback for the in-process guard; also publishes to the bus."""
        with self.db.session() as s:
            s.add(
                SealEventRow(
                    process=str(event.get("process", ""))[:96],
                    dest=str(event.get("dest", ""))[:256],
                    port=event.get("port"),
                    kind=str(event.get("kind", "blocked"))[:24],
                    stack=list(event.get("stack", [])),
                    detail={"pid": event.get("pid")},
                )
            )
        self.bus.publish_threadsafe(
            SealEventNote(
                kind=str(event.get("kind", "blocked")),
                process=str(event.get("process", "")),
                dest=str(event.get("dest", "")),
                port=event.get("port"),
                detail={"stack": event.get("stack", [])},
            )
        )

    def ingest_events_file(self) -> int:
        """Pull guard events written by child processes (JSONL side-channel)."""
        if not self.events_file.is_file():
            return 0
        ingested = 0
        try:
            with self.events_file.open("r", encoding="utf-8") as fh:
                fh.seek(self._offset)
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    with contextlib.suppress(json.JSONDecodeError):
                        self.record_event(json.loads(line))
                        ingested += 1
                self._offset = fh.tell()
        except OSError:
            return ingested
        return ingested

    async def start(self) -> None:
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        self.events_file.touch(exist_ok=True)
        self._task = asyncio.create_task(self._poll_loop(), name="seal-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.ingest_events_file)

    # ------------------------------------------------------------- status

    def blocked_total(self) -> int:
        with self.db.session() as s:
            return int(s.execute(select(func.count()).select_from(SealEventRow)).scalar_one())

    def last_attempts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = (
                s.execute(select(SealEventRow).order_by(SealEventRow.ts.desc()).limit(limit))
                .scalars()
                .all()
            )
            return [
                {
                    "ts": row.ts.isoformat(),
                    "process": row.process,
                    "dest": row.dest,
                    "port": row.port,
                    "kind": row.kind,
                    "stack": row.stack,
                }
                for row in rows
            ]

    def layers(self) -> dict[str, Any]:
        from yantra_server.seal.env import assert_environment_locked
        from yantra_server.seal.socket_guard import is_installed

        return {
            "bundle_verified": False,
            "env_locked": not assert_environment_locked(),
            "socket_guard_active": is_installed(),
            "compose_internal": False,
            "nftables_present": self._nftables_present(),
        }

    def _nftables_present(self) -> bool:
        nft = shutil.which("nft")
        if nft is None:
            return False
        try:
            result = subprocess.run(
                [nft, "list", "table", "inet", "yantra_seal"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def nftables_counters(self) -> dict[str, int]:
        nft = shutil.which("nft")
        if nft is None:
            return {}
        try:
            result = subprocess.run(
                [nft, "-j", "list", "table", "inet", "yantra_seal"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return {}
            data = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return {}
        counters: dict[str, int] = {}
        for item in data.get("nftables", []):
            counter = item.get("counter")
            if counter:
                counters[str(counter.get("name", "drops"))] = int(counter.get("packets", 0))
        return counters

    def status(self) -> dict[str, Any]:
        self.ingest_events_file()
        return {
            "sealed": self.config.sealed(),
            "allowlist": self.config.seal.allowlist,
            "blocked_attempts_total": self.blocked_total(),
            "last_attempts": self.last_attempts(),
            "layers": self.layers(),
            "nftables_counters": self.nftables_counters(),
        }
