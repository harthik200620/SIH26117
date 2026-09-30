"""Single coordinator per local state directory; crash releases the OS lock."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import BinaryIO


class InstanceLock:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "coordinator.lock"
        self.stream: BinaryIO | None = None

    def acquire(self) -> None:
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                locks = importlib.import_module("fcntl")
                locks.flock(stream.fileno(), locks.LOCK_EX | locks.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError(
                "Another BlackBox coordinator owns this data directory. Use one server process per installation."
            ) from exc
        self.stream = stream

    def release(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None
