"""Projects: named directories the agent works in.

Every chat session, task, goal, idea, canvas document and browser belongs to a project; the agent's
file tools, shell and sandbox are confined to that project's directory. Removing a project only
forgets it; its files are never touched.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLM, LLMError
from .store import JsonStore

NAME_SYSTEM = (
    "You name software projects. Given a folder's name, file list and README, reply with ONLY a short, "
    "human-friendly project name of 1-4 words in Title Case. No quotes, no punctuation, no explanation."
)


@dataclass
class Project:
    name: str
    path: str
    description: str = ""
    instructions: str = ""  # added to the agent's system prompt for this project
    color: str = ""         # UI accent, e.g. "#f59e0b"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    @property
    def root(self) -> Path:
        return Path(self.path)


class ProjectError(ValueError):
    pass


def check_path(raw: str, *, create: bool = False) -> Path:
    """Resolve a user-supplied directory. Refuses `/` and the bare home directory: an agent with a
    writable root that broad could rewrite dotfiles or system config."""
    if not str(raw).strip():
        raise ProjectError("path is required")
    p = Path(str(raw).strip()).expanduser()
    if not p.is_absolute():
        raise ProjectError("use an absolute path (or one starting with ~)")
    p = p.resolve()
    if p == Path("/") or p == Path.home().resolve():
        raise ProjectError(f"{p} is too broad to hand to an agent; pick a project folder inside it")
    if not p.exists():
        if not create:
            raise ProjectError(f"{p} does not exist")
        p.mkdir(parents=True)
    if not p.is_dir():
        raise ProjectError(f"{p} is not a directory")
    return p


def fallback_name(path: Path) -> str:
    words = re.split(r"[-_.\s]+", path.name.strip()) or ["Project"]
    return " ".join(w[:1].upper() + w[1:] for w in words if w)[:40] or "Project"


async def suggest_name(llm: LLM, path: Path) -> str:
    """Ask the small model for a name; fall back to the folder name if it is unreachable or rambles."""
    files: list[str] = []
    for child in sorted(path.iterdir())[:60] if path.is_dir() else []:
        if not child.name.startswith("."):
            files.append(child.name + ("/" if child.is_dir() else ""))
    readme = ""
    for cand in ("README.md", "README.rst", "README.txt", "README", "pyproject.toml", "package.json"):
        f = path / cand
        if f.is_file():
            readme = f.read_text(encoding="utf-8", errors="ignore")[:1500]
            break
    prompt = f"Folder: {path.name}\nFiles: {', '.join(files) or '(empty)'}\n\n{readme}"
    try:
        comp = await llm.complete([{"role": "system", "content": NAME_SYSTEM}, {"role": "user", "content": prompt}],
                                  max_tokens=24, temperature=0.3)
    except LLMError:
        return fallback_name(path)
    name = comp.content.strip().splitlines()[0] if comp.content.strip() else ""
    name = re.sub(r"^(project name|name)\s*:\s*", "", name, flags=re.I).strip(" \"'`*.#")
    if " " not in name:  # small models like to answer "Kestrel-Autonomous-Chat"
        name = " ".join(w[:1].upper() + w[1:] for w in re.split(r"[-_]+", name) if w)
    if not name or len(name) > 40 or len(name.split()) > 5:
        return fallback_name(path)
    return name


class ProjectStore(JsonStore[Project]):
    def __init__(self, path: Path):
        super().__init__(path, Project)

    def by_path(self, path: Path) -> Project | None:
        target = str(Path(path).resolve())
        return next((p for p in self.all() if p.path == target), None)

    def ensure(self, path: Path, name: str = "") -> Project:
        """The project for `path`, registering it if new (used for the launch workspace)."""
        path = Path(path).resolve()
        return self.by_path(path) or self.add(Project(name=name or fallback_name(path), path=str(path)))
