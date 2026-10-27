"""File exchange between the controller and the target.

The target exposes a small sandbox directory (``~/rc-share`` by default):

    ~/rc-share/inbox   files uploaded from the controller
    ~/rc-share/outbox  files offered for download to the controller
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize(name: str, fallback: str = "file") -> str:
    name = os.path.basename(name or "").strip()
    name = name.replace("\x00", "")
    name = SAFE_NAME.sub("_", name)
    name = name.lstrip(".") or fallback
    return name[:120]


class FileStore:
    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(root) if root else Path.home() / "rc-share"
        self.inbox = self.root / "inbox"
        self.outbox = self.root / "outbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)

    def _info(self, path: Path, folder: str) -> dict:
        stat = path.stat()
        return {
            "name": path.name,
            "folder": folder,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "age": int(time.time() - stat.st_mtime),
        }

    def list(self) -> dict:
        inbox = [self._info(p, "inbox") for p in sorted(self.inbox.iterdir()) if p.is_file()]
        outbox = [self._info(p, "outbox") for p in sorted(self.outbox.iterdir()) if p.is_file()]
        return {"inbox": inbox, "outbox": outbox}

    def put(self, filename: str, data: bytes, folder: str = "inbox") -> dict:
        target_dir = self.outbox if folder == "outbox" else self.inbox
        name = sanitize(filename)
        path = target_dir / name
        path.write_bytes(data)
        return self._info(path, folder)

    def get(self, name: str, folder: str = "inbox") -> Path:
        safe = sanitize(name)
        base = self.outbox if folder == "outbox" else self.inbox
        path = base / safe
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    def delete(self, name: str, folder: str = "inbox") -> bool:
        try:
            path = self.get(name, folder)
        except FileNotFoundError:
            return False
        path.unlink()
        return True

    def clear(self, folder: str = "inbox") -> int:
        base = self.outbox if folder == "outbox" else self.inbox
        count = 0
        for path in base.iterdir():
            if path.is_file():
                path.unlink()
                count += 1
        return count

    def copy_to_outbox(self, source: os.PathLike | str) -> dict:
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(str(source))
        target = self.outbox / sanitize(path.name)
        shutil.copy2(path, target)
        return self._info(target, "outbox")
