"""A small, durable JSON-file store for dataclass records (projects, goals, ideas, tasks, canvas docs)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Generic, TypeVar

T = TypeVar("T")


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class JsonStore(Generic[T]):
    """Records keyed by `id`, persisted as a JSON list. Unknown keys in old files are ignored and
    missing ones take dataclass defaults, so records survive schema additions."""

    def __init__(self, path: Path, cls: type[T]):
        self.path = Path(path)
        self.cls = cls
        self._lock = threading.RLock()
        self._items: dict[str, T] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        known = {f.name for f in fields(self.cls)}  # type: ignore[arg-type]
        for item in raw if isinstance(raw, list) else []:
            try:
                rec = self.cls(**{k: v for k, v in item.items() if k in known})
            except TypeError:
                continue
            self._items[rec.id] = rec  # type: ignore[attr-defined]

    def _save(self) -> None:
        with self._lock:
            atomic_write(self.path, json.dumps([asdict(r) for r in self._items.values()], ensure_ascii=False, indent=2))  # type: ignore[call-overload]

    def add(self, rec: T) -> T:
        with self._lock:
            self._items[rec.id] = rec  # type: ignore[attr-defined]
            self._save()
        return rec

    def get(self, rid: str) -> T | None:
        """Exact id, or a unique prefix (CLI users type short ids)."""
        if not rid:
            return None
        if rid in self._items:
            return self._items[rid]
        matches = [r for k, r in self._items.items() if k.startswith(rid)]
        return matches[0] if len(matches) == 1 else None

    def update(self, rec: T, **changes: Any) -> T:
        with self._lock:
            for k, v in changes.items():
                setattr(rec, k, v)
            if hasattr(rec, "updated"):
                rec.updated = time.time()  # type: ignore[attr-defined]
            self._save()
        return rec

    def delete(self, rid: str) -> bool:
        with self._lock:
            if self._items.pop(rid, None) is None:
                return False
            self._save()
        return True

    def all(self) -> list[T]:
        return sorted(self._items.values(), key=lambda r: getattr(r, "created", 0))

    def __len__(self) -> int:
        return len(self._items)
