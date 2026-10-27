"""Agent Skills (SKILL.md) with progressive disclosure.

Only name + description are placed in the system prompt. The body is loaded on demand via
the ``load_skill`` tool, and bundled files are listed so the agent can read/run them.
Skill layout:  <dir>/<skill-name>/SKILL.md  (YAML frontmatter: name, description)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str
    files: list[str] = field(default_factory=list)

    def render(self) -> str:
        listing = "\n".join(f"- {f}" for f in self.files) or "(none)"
        return f"# Skill: {self.name}\nBase directory: {self.path.parent}\n\n{self.body.strip()}\n\n## Bundled files\n{listing}"


class SkillRegistry:
    """`skills` holds the enabled skills the agent sees; `found` also includes disabled ones (for the UI).
    `disabled` is shared with the config, so toggling a skill there applies on the next `scan()`."""

    def __init__(self, dirs: list[Path], disabled: list[str] | None = None):
        self.dirs = dirs
        self.disabled = disabled if disabled is not None else []
        self.skills: dict[str, Skill] = {}
        self.found: dict[str, Skill] = {}
        self.errors: list[str] = []

    def scan(self) -> None:
        self.skills.clear()
        self.found.clear()
        self.errors.clear()
        for root in self.dirs:  # later dirs override earlier ones (workspace beats user)
            if not root.is_dir():
                continue
            for skill_md in sorted(root.glob("*/SKILL.md")):
                try:
                    skill = self._parse(skill_md)
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"{skill_md}: {exc}")
                    continue
                self.found[skill.name] = skill
        self.skills.update({n: sk for n, sk in self.found.items() if n not in self.disabled})

    @staticmethod
    def _parse(path: Path) -> Skill:
        text = path.read_text(encoding="utf-8")
        match = _FRONT.match(text)
        if not match:
            raise ValueError("missing YAML frontmatter")
        meta = yaml.safe_load(match.group(1)) or {}
        name, desc = str(meta.get("name", "")).strip(), str(meta.get("description", "")).strip()
        if not name or not desc:
            raise ValueError("frontmatter requires 'name' and 'description'")
        if name != path.parent.name:
            raise ValueError(f"name '{name}' must match directory '{path.parent.name}'")
        files = sorted(
            str(p.relative_to(path.parent)) for p in path.parent.rglob("*") if p.is_file() and p.name != "SKILL.md"
        )[:50]
        return Skill(name=name, description=desc, path=path, body=match.group(2), files=files)

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def index(self) -> str:
        if not self.skills:
            return ""
        return "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())
