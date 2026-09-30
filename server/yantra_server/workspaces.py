"""Local folders explicitly selected by the authenticated workbench user."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


def local_directory(raw: str) -> Path:
    raw = raw.strip()
    if not raw or raw.startswith(("\\\\", "//")):
        raise ValueError("Choose an existing local folder; network shares are not supported")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("Enter the full, absolute folder path")
    path = path.resolve(strict=True)
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("Network shares are not supported")
    if not path.is_dir():
        raise ValueError("Choose a folder, not a file")
    # Check readability without traversing or indexing its contents.
    with os.scandir(path) as entries:
        next(entries, None)
    return path


class WorkspaceFolders:
    def __init__(self, data_dir: Path, roots: list[Path]):
        self.file = data_dir / "workspace-folders.json"
        self.roots = roots
        self.lock = threading.Lock()
        self.saved: list[str] = []
        if self.file.exists():
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(data, list) or not all(isinstance(p, str) for p in data):
                raise ValueError("Invalid saved workspace folder list")
            for raw in data:
                try:
                    path = local_directory(raw)
                except (ValueError, OSError):
                    continue  # A removed or disconnected folder grants no access.
                self.saved.append(str(path))
                if path not in roots:
                    roots.append(path)

    def select(self, raw: str) -> Path:
        path = local_directory(raw)
        with self.lock:
            if path in self.roots:
                return path
            saved = [*self.saved, str(path)]
            self.file.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                dir=self.file.parent, prefix=".workspace-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(saved, output, indent=2)
                os.replace(temporary, self.file)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self.saved = saved
            self.roots.append(path)
        return path
