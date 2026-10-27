"""Agent Skills (SKILL.md) with progressive disclosure.

A skill is a folder:

    skill-name/
      SKILL.md        YAML frontmatter (name, description; optional license, compatibility, metadata,
                      allowed-tools) + instructions
      references/     docs the agent reads only when needed      → read_skill_file
      scripts/        code the agent runs instead of rewriting    → run_skill_script (in the sandbox)
      assets/         templates, images, fonts to put in outputs  → use_skill_asset (copied into the project)

Only name + description sit in the system prompt. `load_skill` loads the body plus an index of the
bundled resources; the resources themselves are loaded, run or copied on demand. Skills usually live
outside the project (~/.cadence/skills), so these tools reach them without opening the rest of $HOME.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # Agent Skills spec: lowercase, digits, single hyphens
RESOURCE_DIRS = ("references", "scripts", "assets")
SKIP_NAMES = {"__MACOSX", ".DS_Store", "__pycache__", ".git", "node_modules", ".venv"}
MAX_FILES = 2000
MAX_TOTAL_BYTES = 100 * 1024 * 1024
INTERPRETERS = {".py": "python3", ".sh": "bash", ".bash": "bash", ".js": "node", ".mjs": "node", ".ts": "npx -y tsx",
                ".rb": "ruby", ".pl": "perl", ".php": "php", ".r": "Rscript", ".R": "Rscript"}


class SkillError(ValueError):
    pass


@dataclass
class Skill:
    name: str
    description: str
    path: Path                       # the SKILL.md file
    body: str
    files: list[str] = field(default_factory=list)  # bundled files, relative to the skill folder
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.path.parent

    def grouped(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {k: [] for k in (*RESOURCE_DIRS, "other")}
        for f in self.files:
            top = f.split("/", 1)[0]
            groups[top if top in RESOURCE_DIRS and "/" in f else "other"].append(f)
        return groups

    def render(self) -> str:
        g = self.grouped()
        out = [f"# Skill: {self.name}", self.body.strip()]
        sections = []
        if g["references"]:
            sections.append("### references/ (read only the ones you need, with read_skill_file)\n" + "\n".join(f"- {f}" for f in g["references"]))
        if g["scripts"]:
            sections.append("### scripts/ (run with run_skill_script; read one only if you must change how you call it)\n"
                            + "\n".join(f"- {f}" for f in g["scripts"]))
        if g["assets"]:
            sections.append("### assets/ (copy into the project with use_skill_asset, then use or edit the copy)\n"
                            + "\n".join(f"- {f}" for f in g["assets"]))
        if g["other"]:
            sections.append("### other files (read_skill_file)\n" + "\n".join(f"- {f}" for f in g["other"]))
        if sections:
            out.append(f"## Bundled resources (skill='{self.name}'; paths are relative to the skill folder)\n\n" + "\n\n".join(sections))
        if self.meta.get("allowed-tools"):
            out.append(f"Tools this skill expects: {self.meta['allowed-tools']}")
        return "\n\n".join(out)

    def resolve(self, rel: str) -> Path:
        """A path inside the skill folder; anything escaping it is refused."""
        target = (self.root / str(rel or "").lstrip("/")).resolve()
        if target != self.root.resolve() and self.root.resolve() not in target.parents:
            raise SkillError(f"'{rel}' is outside the skill '{self.name}'")
        return target


def parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    match = _FRONT.match(text)
    if not match:
        raise SkillError("missing YAML frontmatter (--- name: … description: … ---)")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"frontmatter is not valid YAML: {exc}") from None
    if not isinstance(meta, dict):
        raise SkillError("frontmatter must be a YAML mapping")
    name, desc = str(meta.get("name", "")).strip(), str(meta.get("description", "")).strip()
    if not name or not desc:
        raise SkillError("frontmatter requires 'name' and 'description'")
    if name != path.parent.name:
        raise SkillError(f"name '{name}' must match the folder name '{path.parent.name}'")
    warnings = []
    if not NAME_RE.match(name) or len(name) > 64:
        warnings.append("name should be lowercase letters, digits and single hyphens (max 64)")
    if len(desc) > 1024:
        warnings.append(f"description is {len(desc)} chars; the spec allows 1024")
    files = []
    for p in sorted(path.parent.rglob("*")):
        rel = p.relative_to(path.parent)
        if p.is_file() and p.name != "SKILL.md" and not any(part in SKIP_NAMES for part in rel.parts):
            files.append(rel.as_posix())
    if len(files) > 200:
        warnings.append(f"{len(files)} bundled files; only the first 200 are listed to the agent")
    return Skill(name=name, description=desc, path=path, body=match.group(2), files=files[:200],
                 meta={k: v for k, v in meta.items() if k not in ("name", "description")}, warnings=warnings)


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
                    skill = parse_skill(skill_md)
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"{skill_md}: {exc}")
                    continue
                self.found[skill.name] = skill
        self.skills.update({n: sk for n, sk in self.found.items() if n not in self.disabled})

    _parse = staticmethod(parse_skill)  # pre-0.4 name

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def index(self) -> str:
        if not self.skills:
            return ""
        return "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())


# ─────────────────────────────────────────────────────────────── installing
def _safe_rel(name: str) -> Path | None:
    """A relative path from an archive or upload, or None if it is unsafe or junk."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    if any(p in SKIP_NAMES or p.startswith("._") for p in parts):
        return None
    return Path(*parts)


def unpack(files: dict[Path, bytes], base: Path, *, overwrite: bool = False) -> list[str]:
    """Install every skill found in `files` ({relative path: bytes}) into `base`. A skill is a folder holding
    SKILL.md (at any depth); a lone SKILL.md at the top is named from its frontmatter. Returns names."""
    if len(files) > MAX_FILES:
        raise SkillError(f"too many files ({len(files)}; limit {MAX_FILES})")
    if sum(len(b) for b in files.values()) > MAX_TOTAL_BYTES:
        raise SkillError(f"skill bundle is larger than {MAX_TOTAL_BYTES // 1024 // 1024} MB")
    roots = sorted({p.parent for p in files if p.name == "SKILL.md"}, key=lambda p: (len(p.parts), p.as_posix()))
    roots = [r for r in roots if not any(o != r and o in r.parents for o in roots)]  # nested SKILL.md belong to their parent
    if not roots:
        raise SkillError("no SKILL.md found")
    staged: list[tuple[str, Path, dict[Path, bytes]]] = []
    for root in roots:
        text = files[root / "SKILL.md"].decode("utf-8", "replace")
        m = _FRONT.match(text)
        meta = (yaml.safe_load(m.group(1)) if m else None) or {}
        name = str(meta.get("name", "") if isinstance(meta, dict) else "").strip() or root.name
        if not NAME_RE.match(name) and not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", name):
            raise SkillError(f"invalid skill name {name!r} in {root / 'SKILL.md'}")
        content = {p.relative_to(root): b for p, b in files.items() if root in p.parents}
        staged.append((name, base / name, content))
    for name, dest, _ in staged:
        if dest.exists() and not overwrite:
            raise SkillError(f"a skill named '{name}' already exists in {base} (choose replace to overwrite it)")
    installed = []
    for name, dest, content in staged:
        tmp = base / f".installing-{name}"
        shutil.rmtree(tmp, ignore_errors=True)
        for rel, data in content.items():
            out = tmp / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            if rel.parts[0] == "scripts" or data[:2] == b"#!":
                out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
        try:
            parse_skill(dest / "SKILL.md")
        except SkillError as exc:
            shutil.rmtree(dest)
            raise SkillError(f"{name}: {exc}") from None
        installed.append(name)
    return installed


def read_zip(data: bytes) -> dict[Path, bytes]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillError("not a valid .zip / .skill archive") from None
    out: dict[Path, bytes] = {}
    total = 0
    for info in z.infolist():
        if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:  # skip dirs and symlinks
            continue
        rel = _safe_rel(info.filename)
        if rel is None:
            continue
        total += info.file_size
        if total > MAX_TOTAL_BYTES or len(out) >= MAX_FILES:
            raise SkillError("archive too large to be a skill")
        out[rel] = z.read(info)
    return out


def read_dir(src: Path) -> dict[Path, bytes]:
    out: dict[Path, bytes] = {}
    for p in src.rglob("*"):
        if p.is_file() and not p.is_symlink():
            rel = _safe_rel(p.relative_to(src).as_posix())
            if rel is not None:
                out[(Path(src.name) / rel)] = p.read_bytes()
        if len(out) > MAX_FILES:
            raise SkillError("folder has too many files to be a skill")
    return out


def github_zip_url(url: str) -> tuple[str, str] | None:
    """https://github.com/o/r/tree/<ref>/<path> → (codeload zip URL, path inside the archive)."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/(?:tree|blob)/([^/]+)/?(.*)$", url.strip())
    if not m:
        m2 = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
        if not m2:
            return None
        return f"https://codeload.github.com/{m2.group(1)}/{m2.group(2)}/zip/HEAD", ""
    owner, repo, ref, sub = m.groups()
    sub = re.sub(r"/?SKILL\.md$", "", sub)
    return f"https://codeload.github.com/{owner}/{repo}/zip/{ref}", sub


def subtree(files: dict[Path, bytes], sub: str) -> dict[Path, bytes]:
    """Keep only `sub` (a path inside the repo) from a GitHub archive, whose top folder is '<repo>-<ref>'."""
    if not sub:
        return files
    out = {}
    for p, b in files.items():
        inner = Path(*p.parts[1:]) if len(p.parts) > 1 else p
        try:
            rel = inner.relative_to(sub)
        except ValueError:
            continue
        out[Path(Path(sub).name) / rel] = b
    if not out:
        raise SkillError(f"'{sub}' was not found in that repository")
    return out


def export_zip(skill: Skill) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(skill.root.rglob("*")):
            rel = p.relative_to(skill.root)
            if p.is_file() and not any(part in SKIP_NAMES for part in rel.parts):
                z.write(p, (Path(skill.name) / rel).as_posix())
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────── agent tools
def skill_tools(registry: SkillRegistry, workspace: Any, sandbox: Any, *, timeout: float, output_limit: int,
                allow_shell: bool = True) -> list[Any]:
    from .tools import Tool, ToolError, _schema, truncate

    def skill(name: str) -> Skill:
        s = registry.get(str(name or ""))
        if s is None:
            known = ", ".join(registry.skills) or "none installed"
            raise ToolError(f"unknown skill '{name}'. Available: {known}")
        return s

    def target(s: Skill, rel: str) -> Path:
        try:
            p = s.resolve(rel)
        except SkillError as exc:
            raise ToolError(str(exc)) from None
        if not p.exists():
            # small models often drop the folder ("fill.py" for "scripts/fill.py"): accept a unique basename
            same = [f for f in s.files if f.rsplit("/", 1)[-1] == Path(str(rel)).name]
            if len(same) == 1:
                return s.root / same[0]
            raise ToolError(f"no '{rel}' in skill '{s.name}'. Its files: {', '.join(s.files[:40]) or '(none)'}")
        return p

    async def read_skill_file(args: dict[str, Any]) -> str:
        from .documents import ExtractError, decode, extract, needs_extract

        s = skill(args.get("skill"))
        p = target(s, str(args.get("path", "")))
        if p.is_dir():
            items = sorted(str(c.relative_to(s.root)) + ("/" if c.is_dir() else "") for c in p.iterdir())
            return "\n".join(items) or "(empty folder)"
        with p.open("rb") as fh:
            head = fh.read(8192)
        if needs_extract(p, head):
            try:
                ex = await extract(p, pages=args.get("pages") or None)
            except ExtractError as exc:
                raise ToolError(str(exc)) from None
            text = ex.text
        else:
            text = decode(p.read_bytes())
        lines = text.splitlines()
        start = max(1, int(args.get("offset", 1) or 1))
        limit = int(args.get("limit", 600) or 600)
        chunk = lines[start - 1: start - 1 + limit]
        more = f"\n…[{len(lines) - (start - 1 + len(chunk))} more lines; use offset]" if start - 1 + len(chunk) < len(lines) else ""
        return truncate("\n".join(chunk) + more, output_limit)

    async def run_skill_script(args: dict[str, Any]) -> str:
        import shlex

        from .sandbox import SandboxError

        s = skill(args.get("skill"))
        script = target(s, str(args.get("script", "")))
        if script.is_dir():
            raise ToolError("script must be a file, e.g. scripts/convert.py")
        raw = args.get("args") or []
        argv = shlex.split(raw) if isinstance(raw, str) else [str(a) for a in raw]
        interp = INTERPRETERS.get(script.suffix)
        if interp is None and not os.access(script, os.X_OK):
            raise ToolError(f"don't know how to run {script.name}: give it a known extension ({', '.join(sorted(INTERPRETERS))}) or a shebang")
        cmd = (f"{interp} " if interp else "") + " ".join(shlex.quote(x) for x in [str(script), *argv])
        env = {"SKILL_DIR": str(s.root), "SKILL_NAME": s.name}
        try:
            res = await sandbox.run(cmd, timeout=float(args.get("timeout") or timeout), env=env, ro=[str(s.root)])
        except SandboxError as exc:
            raise ToolError(f"sandbox unavailable: {exc}") from None
        if res.timed_out:
            return "ERROR: script timed out (pass timeout=seconds to allow longer)"
        return truncate(f"exit {res.exit}\n{res.output}", output_limit)

    async def use_skill_asset(args: dict[str, Any]) -> str:
        s = skill(args.get("skill"))
        src = target(s, str(args.get("path", "")))
        dest = workspace.resolve(str(args.get("dest") or src.name))
        if dest.exists() and not args.get("overwrite"):
            raise ToolError(f"{workspace.rel(dest)} exists; pass overwrite=true to replace it")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
        return f"copied {s.name}/{src.relative_to(s.root)} → {workspace.rel(dest)}"

    sk = {"type": "string", "description": "skill name"}
    tools = [
        Tool("read_skill_file", "Read a file bundled with a skill (references/, scripts/, assets/ …), given the skill name "
             "and a path relative to the skill folder. PDFs, Office docs and spreadsheets are converted to text. "
             "A folder path lists its contents.",
             _schema({"skill": sk, "path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"},
                      "pages": {"type": "string"}}, ["skill", "path"]), read_skill_file, source="skills", read_only=True),
        Tool("use_skill_asset", "Copy a skill's asset (file or folder, e.g. assets/template.docx) into the project so "
             "you can use or edit it there.",
             _schema({"skill": sk, "path": {"type": "string"}, "dest": {"type": "string", "description": "path in the project"},
                      "overwrite": {"type": "boolean"}}, ["skill", "path"]), use_skill_asset, source="skills"),
    ]
    if allow_shell:
        tools.append(Tool(
            "run_skill_script", "Run a script bundled with a skill (e.g. scripts/fill_form.py) with arguments. It runs in the "
            "sandbox with the project as working directory (so outputs land in the project), the skill folder readable, "
            "and $SKILL_DIR pointing at it. Interpreter is picked from the extension (.py .sh .js .ts .rb …).",
            _schema({"skill": sk, "script": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}},
                     "timeout": {"type": "number"}}, ["skill", "script"]), run_skill_script, source="skills"))
    return tools
