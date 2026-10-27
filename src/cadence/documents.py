"""Read any file: turn it into text (and tables) a model can use, or images it can look at.

    extract(path)  → Extract(kind, text, tables, pages, meta)
    render_pages(path, pages) → PNG files of PDF pages / office slides, for the vision model

Handled:
  text & code   anything UTF-8 (py, ts/tsx, js/jsx, css, html, xml, md, yaml, toml, sql, logs …)
  data          json / jsonld (validated, outlined), yaml, csv / tsv (table + column stats)
  spreadsheets  xlsx / xlsm (openpyxl), xls (xlrd), ods (via LibreOffice)
  documents     pdf (pypdf, per page; scanned pages via OCR by the vision model), docx, pptx, odt/odp,
                epub, rtf, ipynb; legacy doc / ppt via LibreOffice when installed
  images        png / jpg / webp / gif / bmp / tiff / ico … (size; described by the vision model on request)
  containers    zip / tar / gz archives (listing), sqlite (schema, counts, sample rows)
  media         audio / video (ffprobe metadata)
  anything else `file` type, printable strings and a hex preview
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import html
import io
import json
import re
import shutil
import sqlite3
import tarfile
import zipfile
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

MAX_ROWS = 500           # rows kept per table
SAMPLE_ROWS = 40         # rows rendered into the model-facing text
TEXT_SNIFF = 8192

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".ico", ".heic", ".avif"}
MEDIA_EXT = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".mp4", ".mkv", ".mov", ".webm", ".avi"}
OFFICE_LEGACY = {".doc", ".ppt", ".rtf", ".odt", ".odp", ".ods", ".xls", ".wpd", ".pages", ".key", ".numbers"}


DOCUMENT_EXT = ({".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".epub", ".ipynb", ".db", ".sqlite", ".sqlite3", ".zip", ".jar",
                 ".whl", ".tar", ".tgz", ".gz", ".bz2", ".xz"} | OFFICE_LEGACY | IMAGE_EXT | MEDIA_EXT)


def needs_extract(path: Path, head: bytes) -> bool:
    """True when a file must be converted rather than shown as source: by type first (a PDF can be pure
    ASCII), then by content."""
    return path.suffix.lower() in DOCUMENT_EXT or not is_text(head)


@dataclass
class Table:
    name: str
    columns: list[str]
    rows: list[list[Any]]
    total_rows: int


@dataclass
class Extract:
    kind: str                     # text | code | data | table | document | pdf | image | archive | database | media | binary
    format: str                   # e.g. "csv", "docx", "pdf"
    text: str                     # model-ready rendering
    tables: list[Table] = field(default_factory=list)
    pages: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExtractError(ValueError):
    pass


# ───────────────────────────────────────────────────────────────── helpers
def is_text(raw: bytes) -> bool:
    if b"\0" in raw[:TEXT_SNIFF]:
        return False
    try:
        raw[:TEXT_SNIFF].decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start > min(len(raw), TEXT_SNIFF) - 4
    return True


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).replace("\n", " ").replace("|", "\\|")


def markdown_table(t: Table, limit: int = SAMPLE_ROWS) -> str:
    if not t.columns and not t.rows:
        return "(empty)"
    cols = t.columns or [f"col{i + 1}" for i in range(max(len(r) for r in t.rows))]
    out = ["| " + " | ".join(_cell(c) for c in cols) + " |", "|" + "---|" * len(cols)]
    for r in t.rows[:limit]:
        r = list(r) + [""] * (len(cols) - len(r))
        out.append("| " + " | ".join(_cell(c) for c in r[: len(cols)]) + " |")
    if t.total_rows > limit:
        out.append(f"… {t.total_rows - limit} more rows")
    return "\n".join(out)


def column_stats(t: Table) -> str:
    """One line per column: type guess, blanks, and range or most common values."""
    lines = []
    for i, name in enumerate(t.columns[:40]):
        vals = [r[i] for r in t.rows if i < len(r) and r[i] not in (None, "")]
        nums = []
        for v in vals:
            try:
                nums.append(float(str(v).replace(",", "")))
            except ValueError:
                pass
        blanks = len(t.rows) - len(vals)
        if vals and len(nums) >= 0.9 * len(vals):
            lines.append(f"- {name}: numeric, min {min(nums):g}, max {max(nums):g}, mean {sum(nums) / len(nums):.4g}"
                         + (f", {blanks} blank" if blanks else ""))
        else:
            counts: dict[str, int] = {}
            for v in vals:
                counts[str(v)] = counts.get(str(v), 0) + 1
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
            distinct = len(counts)
            lines.append(f"- {name}: text, {distinct} distinct" + (f", {blanks} blank" if blanks else "")
                         + (f"; top: {', '.join(f'{k[:30]} ({n})' for k, n in top)}" if top and distinct < len(vals) else ""))
    return "\n".join(lines)


def render_tables(tables: list[Table], header: str = "") -> str:
    parts = [header] if header else []
    for t in tables:
        parts.append(f"## {t.name}: {t.total_rows} rows × {len(t.columns)} columns")
        if t.rows:
            parts.append("Columns:\n" + column_stats(t))
        parts.append(markdown_table(t))
    return "\n\n".join(parts)


def parse_pages(spec: str | None, total: int) -> list[int]:
    """'3', '2-5', '1,4,7-9' → 1-based page numbers within range (default: all)."""
    if not spec:
        return list(range(1, total + 1))
    out: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        out += [p for p in range(lo, hi + 1) if 1 <= p <= total]
    return out


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer", "pre"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
            if tag.startswith("h") and len(tag) == 2:
                self.out.append("#" * int(tag[1]) + " ")
            elif tag == "li":
                self.out.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(src: str) -> str:
    p = _HTMLText()
    p.feed(src)
    text = html.unescape("".join(p.out))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


async def _run(*argv: str, timeout: float = 90.0) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                stdin=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, b"timed out"
    return proc.returncode or 0, out


def cache_dir() -> Path:
    from .config import HOME_DIR
    d = HOME_DIR / "state" / "cache" / "docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{path.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]


async def soffice_convert(path: Path, to: str) -> Path:
    """Convert with LibreOffice (headless) into the cache. Raises ExtractError if unavailable."""
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise ExtractError(f"reading {path.suffix} files needs LibreOffice (apt install libreoffice)")
    out = cache_dir() / f"{_key(path)}-{to}"
    target = out / f"{path.stem}.{to.split(':')[0]}"
    if target.is_file():
        return target
    out.mkdir(parents=True, exist_ok=True)
    profile = cache_dir() / "lo-profile"  # private profile: works while the user has LibreOffice open
    code, msg = await _run(exe, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", to,
                           "--outdir", str(out), str(path), timeout=120)
    if code != 0 or not target.is_file():
        raise ExtractError(f"LibreOffice could not convert {path.name}: {msg.decode(errors='replace')[:200]}")
    return target


# ─────────────────────────────────────────────────────────────── formats
def _json(path: Path, text: str) -> Extract:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        if path.suffix.lower() in (".jsonl", ".ndjson") or "\n{" in text:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()][:MAX_ROWS]
            return _records(path, rows, "jsonl", len(text.splitlines()))
        return Extract("data", "json", text, warning=f"invalid JSON: {exc}")
    outline = _outline(data)
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data[:50]):
        ex = _records(path, data, path.suffix.lstrip(".") or "json", len(data))
        ex.text = f"JSON array of {len(data)} objects.\nStructure:\n{outline}\n\n{ex.text}"
        return ex
    pretty = json.dumps(data, indent=2, ensure_ascii=False)
    return Extract("data", path.suffix.lstrip(".") or "json", f"Structure:\n{outline}\n\n{pretty}",
                   meta={"top": type(data).__name__})


def _records(path: Path, rows: list[Any], fmt: str, total: int) -> Extract:
    cols: list[str] = []
    for r in rows[:200]:
        for k in (r if isinstance(r, dict) else {}):
            if k not in cols:
                cols.append(str(k))
    table = Table(path.name, cols, [[_flat(r.get(c)) if isinstance(r, dict) else _flat(r) for c in cols] for r in rows[:MAX_ROWS]], total)
    return Extract("table", fmt, render_tables([table]), tables=[table])


def _flat(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def _outline(data: Any, depth: int = 0, key: str = "$") -> str:
    pad = "  " * depth
    if depth > 4:
        return f"{pad}{key}: …"
    if isinstance(data, dict):
        lines = [f"{pad}{key}: object ({len(data)} keys)"]
        for k, v in list(data.items())[:30]:
            lines.append(_outline(v, depth + 1, str(k)))
        if len(data) > 30:
            lines.append(f"{pad}  … {len(data) - 30} more keys")
        return "\n".join(lines)
    if isinstance(data, list):
        first = _outline(data[0], depth + 1, "[0]") if data else ""
        return f"{pad}{key}: array ({len(data)})" + (f"\n{first}" if first else "")
    return f"{pad}{key}: {type(data).__name__}" + (f" = {str(data)[:40]!r}" if depth < 3 else "")


def _yaml(path: Path, text: str) -> Extract:
    import yaml
    try:
        docs = [d for d in yaml.safe_load_all(text)]
    except yaml.YAMLError as exc:
        return Extract("data", "yaml", text, warning=f"invalid YAML: {str(exc)[:200]}")
    outline = "\n".join(_outline(d) for d in docs[:5])
    return Extract("data", "yaml", f"Structure:\n{outline}\n\n{text}", meta={"documents": len(docs)})


def _delimited(path: Path, text: str) -> Extract:
    sample = text[:20000]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return Extract("table", path.suffix.lstrip("."), "(empty)")
    try:
        header = csv.Sniffer().has_header(sample)
    except csv.Error:
        header = True
    cols = rows[0] if header else [f"col{i + 1}" for i in range(len(rows[0]))]
    body = rows[1:] if header else rows
    t = Table(path.name, cols, body[:MAX_ROWS], len(body))
    return Extract("table", path.suffix.lstrip(".") or "csv", render_tables([t]), tables=[t])


def _xlsx(path: Path, sheet: str | None) -> Extract:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    tables = []
    names = wb.sheetnames
    for name in names:
        if sheet and name != sheet:
            continue
        ws = wb[name]
        rows = []
        total = 0
        for row in ws.iter_rows(values_only=True):
            if not any(v not in (None, "") for v in row):
                continue
            total += 1
            if len(rows) <= MAX_ROWS:
                rows.append(list(row))
        width = max((max((i for i, v in enumerate(r) if v not in (None, "")), default=-1) for r in rows), default=-1) + 1
        rows = [r[:width] for r in rows]  # read-only sheets often report thousands of empty trailing columns
        cols = [_cell(c) or f"col{i + 1}" for i, c in enumerate(rows[0])] if rows else []
        tables.append(Table(name, cols, rows[1:], max(0, total - 1)))
    wb.close()
    return Extract("table", path.suffix.lstrip("."), render_tables(tables, f"Workbook with {len(names)} sheet(s): {', '.join(names)}"),
                   tables=tables, meta={"sheets": names})


def _xls(path: Path, sheet: str | None) -> Extract:
    import xlrd
    book = xlrd.open_workbook(str(path), on_demand=True)
    tables = []
    for sh in book.sheets():
        if sheet and sh.name != sheet:
            continue
        rows = [sh.row_values(i) for i in range(min(sh.nrows, MAX_ROWS + 1))]
        cols = [_cell(c) or f"col{i + 1}" for i, c in enumerate(rows[0])] if rows else []
        tables.append(Table(sh.name, cols, rows[1:], max(0, sh.nrows - 1)))
    names = book.sheet_names()
    return Extract("table", "xls", render_tables(tables, f"Workbook with {len(names)} sheet(s): {', '.join(names)}"),
                   tables=tables, meta={"sheets": names})


def _pdf(path: Path, pages: str | None) -> Extract:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise ExtractError("the PDF is password-protected") from None
    total = len(reader.pages)
    want = parse_pages(pages, total)
    parts, empty = [], []
    for n in want:
        try:
            text = reader.pages[n - 1].extract_text() or ""
        except Exception as exc:  # noqa: BLE001 — one broken page must not lose the rest
            text = f"(could not extract: {exc})"
        if not text.strip():
            empty.append(n)
        parts.append(f"--- page {n} of {total} ---\n{text.strip()}")
    meta = {k.lstrip("/"): str(v) for k, v in (reader.metadata or {}).items() if v}
    warning = ""
    if empty:
        warning = (f"{len(empty)} page(s) have no text layer (scanned?): {', '.join(map(str, empty[:20]))}. "
                   "Use view_image on the PDF with page=N to read them with the vision model.")
    return Extract("pdf", "pdf", "\n\n".join(parts), pages=total, meta={**meta, "scanned_pages": empty}, warning=warning)


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _docx(path: Path) -> Extract:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    out: list[str] = []
    tables: list[Table] = []
    for el in list(body) if body is not None else []:
        if el.tag == f"{W}p":
            text = "".join(t.text or "" for t in el.iter(f"{W}t"))
            if not text.strip():
                continue
            style = el.find(f"{W}pPr/{W}pStyle")
            sv = (style.get(f"{W}val") if style is not None else "") or ""
            m = re.match(r"(?i)heading\s*(\d)", sv)
            if m:
                out.append("#" * int(m.group(1)) + " " + text)
            elif sv.lower() == "title":
                out.append("# " + text)
            elif el.find(f"{W}pPr/{W}numPr") is not None or "list" in sv.lower():
                out.append("- " + text)
            else:
                out.append(text)
        elif el.tag == f"{W}tbl":
            rows = [["".join(t.text or "" for t in cell.iter(f"{W}t")) for cell in tr.findall(f"{W}tc")] for tr in el.findall(f"{W}tr")]
            if rows:
                t = Table(f"Table {len(tables) + 1}", rows[0], rows[1:], len(rows) - 1)
                tables.append(t)
                out.append(markdown_table(t, 200))
    return Extract("document", "docx", "\n\n".join(out), tables=tables)


def _pptx(path: Path) -> Extract:
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out = []
        for i, name in enumerate(slides, 1):
            root = ET.fromstring(z.read(name))
            paras = ["".join(t.text or "" for t in p.iter(f"{A}t")) for p in root.iter(f"{A}p")]
            text = "\n".join(p for p in paras if p.strip())
            notes_name = name.replace("slides/slide", "notesSlides/notesSlide")
            notes = ""
            if notes_name in z.namelist():
                nroot = ET.fromstring(z.read(notes_name))
                notes = " ".join(t.text or "" for t in nroot.iter(f"{A}t")).strip()
            out.append(f"## Slide {i}\n{text}" + (f"\n\nSpeaker notes: {notes}" if notes else ""))
    return Extract("document", "pptx", "\n\n".join(out), pages=len(slides))


def _opendoc(path: Path) -> Extract:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))
    ns = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    out = []
    for el in root.iter():
        if el.tag in (f"{ns}h", f"{ns}p"):
            text = "".join(el.itertext()).strip()
            if text:
                level = int(el.get(f"{ns}outline-level", "1")) if el.tag == f"{ns}h" else 0
                out.append(("#" * level + " " if level else "") + text)
    return Extract("document", path.suffix.lstrip("."), "\n\n".join(out))


def _epub(path: Path) -> Extract:
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if n.endswith((".xhtml", ".html", ".htm")))
        parts = [html_to_text(z.read(n).decode("utf-8", "replace")) for n in names]
    return Extract("document", "epub", "\n\n".join(p for p in parts if p), pages=len(names))


def _rtf(text: str) -> Extract:
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\{\\\*[^{}]*\}", "", text)
    text = re.sub(r"\\'([0-9a-f]{2})", lambda m: bytes.fromhex(m.group(1)).decode("latin-1"), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", "", text)
    return Extract("document", "rtf", re.sub(r"\n{3,}", "\n\n", text).strip())


def _ipynb(text: str) -> Extract:
    nb = json.loads(text)
    out = []
    for i, c in enumerate(nb.get("cells", []), 1):
        src = "".join(c.get("source", []))
        if c.get("cell_type") == "code":
            out.append(f"### In [{i}]\n```python\n{src}\n```")
            for o in c.get("outputs", [])[:3]:
                txt = "".join(o.get("text", []) or (o.get("data", {}) or {}).get("text/plain", []))
                if txt:
                    out.append(f"Out:\n```\n{txt[:2000]}\n```")
                elif "image/png" in (o.get("data") or {}):
                    out.append("Out: [image]")
        else:
            out.append(src)
    return Extract("document", "ipynb", "\n\n".join(out), pages=len(nb.get("cells", [])))


def _image(path: Path) -> Extract:
    meta: dict[str, Any] = {}
    try:
        from PIL import Image
        with Image.open(path) as im:
            meta = {"width": im.width, "height": im.height, "mode": im.mode, "format": im.format,
                    "frames": getattr(im, "n_frames", 1)}
    except Exception:  # noqa: BLE001
        pass
    dims = f"{meta['width']}×{meta['height']} " if meta.get("width") else ""
    return Extract("image", path.suffix.lstrip(".").lower(),
                   f"Image {path.name}: {dims}{meta.get('format', '')}. Use view_image to have the vision model look at it.", meta=meta)


def _archive(path: Path) -> Extract:
    rows: list[list[Any]] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            rows = [[i.filename, i.file_size, i.compress_size] for i in z.infolist()]
        cols = ["path", "size", "compressed"]
    else:
        with tarfile.open(path) as t:
            rows = [[m.name, m.size, "dir" if m.isdir() else "file"] for m in t.getmembers()]
        cols = ["path", "size", "type"]
    t = Table(path.name, cols, rows[:MAX_ROWS], len(rows))
    total = sum(r[1] for r in rows if isinstance(r[1], int))
    return Extract("archive", path.suffix.lstrip("."), f"Archive with {len(rows)} entries, {total:,} bytes uncompressed.\n\n" + markdown_table(t, 200),
                   tables=[t])


def _sqlite(path: Path) -> Extract:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        objs = con.execute("select name, type, sql from sqlite_master where type in ('table','view') and name not like 'sqlite_%'").fetchall()
        parts, tables = [], []
        for name, typ, sql in objs:
            count = con.execute(f'select count(*) from "{name}"').fetchone()[0]
            cur = con.execute(f'select * from "{name}" limit 20')
            cols = [d[0] for d in cur.description]
            t = Table(name, cols, [list(r) for r in cur.fetchall()], count)
            tables.append(t)
            parts.append(f"## {typ} {name} ({count} rows)\n```sql\n{sql}\n```\n{markdown_table(t, 5)}")
    finally:
        con.close()
    return Extract("database", "sqlite", "\n\n".join(parts) or "(no tables)", tables=tables)


async def _media(path: Path) -> Extract:
    meta: dict[str, Any] = {}
    if shutil.which("ffprobe"):
        code, out = await _run("ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path), timeout=30)
        if code == 0:
            info = json.loads(out or b"{}")
            fmt = info.get("format", {})
            meta = {"duration_s": round(float(fmt.get("duration", 0) or 0), 2), "bit_rate": fmt.get("bit_rate"),
                    "streams": [{k: s.get(k) for k in ("codec_type", "codec_name", "width", "height", "sample_rate", "channels")
                                 if s.get(k) is not None} for s in info.get("streams", [])],
                    "tags": fmt.get("tags", {})}
    return Extract("media", path.suffix.lstrip("."), f"Media file {path.name}\n{json.dumps(meta, indent=2)}", meta=meta)


async def _binary(path: Path, raw: bytes) -> Extract:
    kind = ""
    if shutil.which("file"):
        code, out = await _run("file", "-b", str(path), timeout=10)
        kind = out.decode(errors="replace").strip() if code == 0 else ""
    strings = re.findall(rb"[\x20-\x7e]{6,}", raw[:200_000])[:60]
    hexdump = "\n".join(f"{i:08x}  {raw[i:i + 16].hex(' ')}" for i in range(0, min(len(raw), 256), 16))
    text = (f"Binary file {path.name} ({len(raw):,} bytes){': ' + kind if kind else ''}\n\nPrintable strings:\n"
            + "\n".join(s.decode() for s in strings) + f"\n\nHex (first 256 bytes):\n{hexdump}")
    return Extract("binary", path.suffix.lstrip(".") or "bin", text, meta={"type": kind, "size": len(raw)})


CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".scss", ".less", ".html", ".htm", ".xml", ".svg",
            ".sh", ".bash", ".zsh", ".rs", ".go", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php",
            ".swift", ".lua", ".sql", ".r", ".scala", ".vue", ".svelte", ".toml", ".ini", ".cfg", ".dockerfile", ".gradle"}


async def extract(path: Path, *, pages: str | None = None, sheet: str | None = None, raw_text: bool = False) -> Extract:
    """Convert any file to text/tables. `raw_text` keeps source files verbatim (no outline/table rendering)."""
    path = Path(path)
    if not path.is_file():
        raise ExtractError(f"not a file: {path}")
    suf = path.suffix.lower()
    size = path.stat().st_size
    try:
        if suf in IMAGE_EXT:
            return _image(path)
        if suf == ".pdf":
            return await asyncio.to_thread(_pdf, path, pages)
        if suf in (".xlsx", ".xlsm"):
            return await asyncio.to_thread(_xlsx, path, sheet)
        if suf == ".xls":
            return await asyncio.to_thread(_xls, path, sheet)
        if suf == ".docx":
            return await asyncio.to_thread(_docx, path)
        if suf == ".pptx":
            return await asyncio.to_thread(_pptx, path)
        if suf in (".odt", ".odp"):
            return await asyncio.to_thread(_opendoc, path)
        if suf == ".epub":
            return await asyncio.to_thread(_epub, path)
        if suf == ".ods":
            return await asyncio.to_thread(_xlsx, await soffice_convert(path, "xlsx"), sheet)
        if suf in (".doc", ".wpd", ".pages"):
            return await asyncio.to_thread(_docx, await soffice_convert(path, "docx"))
        if suf in (".ppt", ".key"):
            return await asyncio.to_thread(_pptx, await soffice_convert(path, "pptx"))
        if suf in (".db", ".sqlite", ".sqlite3"):
            return await asyncio.to_thread(_sqlite, path)
        if suf in MEDIA_EXT:
            return await _media(path)
        if suf in (".zip", ".jar", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".whl") and (zipfile.is_zipfile(path) or tarfile.is_tarfile(path)):
            return await asyncio.to_thread(_archive, path)
    except ExtractError:
        raise
    except Exception as exc:  # noqa: BLE001 — a corrupt file is a readable error, not a crash
        raise ExtractError(f"could not read {path.name} as {suf or 'this type'}: {exc.__class__.__name__}: {exc}") from None

    raw = await asyncio.to_thread(path.read_bytes)
    if raw[:16].startswith(b"SQLite format 3"):
        return await asyncio.to_thread(_sqlite, path)
    if not is_text(raw):
        return await _binary(path, raw)
    text = decode(raw)
    if raw_text:
        return Extract("code" if suf in CODE_EXT else "text", suf.lstrip(".") or "txt", text)
    if suf in (".json", ".jsonld", ".geojson", ".jsonl", ".ndjson", ".webmanifest") or (suf == "" and text.lstrip()[:1] in "{["):
        return _json(path, text)
    if suf in (".yaml", ".yml"):
        return _yaml(path, text)
    if suf in (".csv", ".tsv", ".psv"):
        return _delimited(path, text)
    if suf == ".ipynb":
        return _ipynb(text)
    if suf == ".rtf" or text.startswith("{\\rtf"):
        return _rtf(text)
    if suf in (".html", ".htm") and size > 0:
        return Extract("code", "html", text, meta={"readable": html_to_text(text)[:20000]})
    return Extract("code" if suf in CODE_EXT else "text", suf.lstrip(".") or "txt", text)


# ─────────────────────────────────────────────────────────────── visuals
VISION_MIME_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
RENDERABLE = {".docx", ".pptx", ".xlsx", ".xlsm", ".odg"} | OFFICE_LEGACY  # → PDF via LibreOffice → page image


async def to_viewable(path: Path, page: int = 1) -> Path:
    """A PNG/JPEG the vision model accepts: images as-is (other formats converted), PDF pages and office
    documents rendered (office → PDF via LibreOffice → page)."""
    suf = path.suffix.lower()
    if suf in VISION_MIME_EXT:
        return path
    if suf in IMAGE_EXT:
        out = cache_dir() / f"{_key(path)}.png"
        if not out.is_file():
            from PIL import Image
            with Image.open(path) as im:
                im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "PNG")
        return out
    if suf == ".svg":
        raise ExtractError("SVG is XML text: read_file shows its source; open it in the browser to see it rendered")
    if suf != ".pdf" and suf not in RENDERABLE:
        with path.open("rb") as fh:
            text = is_text(fh.read(TEXT_SNIFF))
        raise ExtractError(f"{path.name} is a text file: use read_file" if text else
                           f"cannot show {suf or 'this file type'} as an image; read_file describes it")
    pdf = path if suf == ".pdf" else await soffice_convert(path, "pdf")
    if not shutil.which("pdftoppm"):
        raise ExtractError("rendering pages needs pdftoppm (apt install poppler-utils)")
    out = cache_dir() / f"{_key(pdf)}-p{page}"
    png = out.with_suffix(".png")
    if not png.is_file():
        code, msg = await _run("pdftoppm", "-png", "-r", "110", "-f", str(page), "-l", str(page), "-singlefile", str(pdf), str(out), timeout=60)
        if code != 0 or not png.is_file():
            raise ExtractError(f"could not render page {page}: {msg.decode(errors='replace')[:200]}")
    return png
