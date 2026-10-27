"""One-time migration from Kestrel (≤0.3) to Cadence.

Runs at startup and is a no-op once done:
  * ~/.kestrel → ~/.cadence (moved, not copied; a symlink is left at the old path so old scripts and
    hook commands that mention it keep working)
  * <project>/.kestrel → <project>/.cadence for every project (skills, plugins, attachments, config)
  * plugin source: `kestrel` imports rewritten to `cadence` (original kept as <file>.kestrel-bak)
  * config.yaml / settings.json / session transcripts: `.kestrel` paths rewritten to `.cadence`
  * Qdrant: the existing `kestrel_memory` collection is kept (pinned in settings.json), so vectors
    are not rebuilt
  * KESTREL_* environment variables are honoured as fallbacks for CADENCE_* (see config.py)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

LEGACY_DIR = ".kestrel"
DIR = ".cadence"
_IMPORT = re.compile(r"\b(from|import)\s+kestrel\b")


def _rewrite(path: Path, fn) -> bool:
    try:
        old = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    new = fn(old)
    if new == old:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def _paths(text: str) -> str:
    return text.replace(f"/{LEGACY_DIR}/", f"/{DIR}/").replace(f"{LEGACY_DIR}/", f"{DIR}/").replace(f"/{LEGACY_DIR}\"", f"/{DIR}\"")


def rewrite_plugins(folder: Path) -> list[str]:
    """Point plugin imports at the renamed package, keeping a backup of each file changed."""
    changed = []
    if not folder.is_dir():
        return changed
    for py in folder.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        if not _IMPORT.search(text) and "kestrel." not in text:
            continue
        backup = py.with_name(py.name + ".kestrel-bak")
        if not backup.exists():
            shutil.copy2(py, backup)
        new = _IMPORT.sub(lambda m: f"{m.group(1)} cadence", text).replace("kestrel.", "cadence.")
        py.write_text(new, encoding="utf-8")
        changed.append(str(py))
    return changed


def _only_cache(home: Path) -> bool:
    """True if `home` holds nothing but regenerable cache (e.g. created by a tool before the migration ran)."""
    cache = home / "state" / "cache"
    return all(p == cache or cache in p.parents or p in (home / "state",) for p in home.rglob("*"))


def _merge_move(src: Path, dst: Path) -> None:
    for child in list(src.iterdir()):
        target = dst / child.name
        if not target.exists():
            shutil.move(str(child), str(target))
        elif child.is_dir() and target.is_dir():
            _merge_move(child, target)
    shutil.rmtree(src, ignore_errors=True)


def migrate_home(home: Path, legacy: Path | None = None) -> list[str]:
    """Move the legacy home to `home` unless `home` already holds real data. Returns what was done."""
    legacy = legacy or Path.home() / LEGACY_DIR
    if legacy.is_symlink() or not legacy.is_dir() or (home.exists() and home.resolve() == legacy.resolve()):
        return []
    if home.exists() and not _only_cache(home):
        return []  # both exist with data: never overwrite; the user decides
    done = [f"moved {legacy} → {home}"]
    if home.exists():
        _merge_move(legacy, home)
    else:
        shutil.move(str(legacy), str(home))
    try:
        legacy.symlink_to(home, target_is_directory=True)
        done.append(f"left a symlink at {legacy}")
    except OSError:
        pass
    for name in ("config.yaml", "settings.json"):
        if _rewrite(home / name, _paths):
            done.append(f"updated paths in {name}")
    done += [f"rewrote imports in {p}" for p in rewrite_plugins(home / "plugins")]
    sessions = home / "state" / "sessions"
    n = sum(_rewrite(f, _paths) for f in sessions.glob("*.json")) if sessions.is_dir() else 0
    if n:
        done.append(f"updated attachment paths in {n} transcript(s)")
    _keep_vectors(home, done)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "migrated-from-kestrel.json").write_text(json.dumps({"at": time.time(), "done": done}, indent=2))
    for line in done:
        log.warning("migration: %s", line)
    return done


def _keep_vectors(home: Path, done: list[str]) -> None:
    """Qdrant data lives in the server, not in the home folder: keep using the old collection."""
    import yaml

    cfg_file, settings_file = home / "config.yaml", home / "settings.json"
    try:
        cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {} if cfg_file.is_file() else {}
        settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.is_file() else {}
    except (OSError, ValueError, yaml.YAMLError):
        return
    backend = (settings.get("memory") or {}).get("backend") or (cfg.get("memory") or {}).get("backend")
    if backend == "qdrant" and not (settings.get("memory") or {}).get("collection") and not (cfg.get("memory") or {}).get("collection"):
        settings.setdefault("memory", {})["collection"] = "kestrel_memory"
        settings_file.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
        done.append("kept the existing Qdrant collection 'kestrel_memory'")


def migrate_project(root: Path) -> list[str]:
    """<project>/.kestrel → <project>/.cadence."""
    legacy, new = Path(root) / LEGACY_DIR, Path(root) / DIR
    if not legacy.is_dir() or legacy.is_symlink() or new.exists():
        return []
    try:
        legacy.rename(new)
    except OSError as exc:
        log.warning("could not migrate %s: %s", legacy, exc)
        return []
    done = [f"moved {legacy} → {new}"]
    done += [f"rewrote imports in {p}" for p in rewrite_plugins(new / "plugins")]
    for line in done:
        log.warning("migration: %s", line)
    return done


def legacy_env() -> None:
    """Copy KESTREL_* variables to CADENCE_* when the new name is unset."""
    for key, value in list(os.environ.items()):
        if key.startswith("KESTREL_"):
            os.environ.setdefault("CADENCE_" + key.removeprefix("KESTREL_"), value)
