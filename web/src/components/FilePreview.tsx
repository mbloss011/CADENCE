import clsx from "clsx";
import { AlertTriangle, Eye, FileText, Loader2, Table2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type Preview, type PreviewTable } from "../lib/api";
import { Markdown } from "./Messages";
import { Busy } from "./ui";

const TABLE_EXT = /\.(csv|tsv|psv|xlsx|xlsm|xls|ods|db|sqlite3?|zip|jar|whl|tar|tgz|gz|bz2|xz|jsonl|ndjson)$/i;
const DOC_EXT = /\.(pdf|docx?|pptx?|odt|odp|rtf|epub|ipynb|key|pages|wpd)$/i;
const MEDIA_EXT = /\.(mp3|wav|ogg|flac|m4a|aac|mp4|webm|mov|mkv|avi)$/i;
const DATA_EXT = /\.(json|jsonld|geojson|ya?ml|webmanifest)$/i;
const VIDEO_EXT = /\.(mp4|webm|mov|mkv|avi)$/i;
export const LOOKABLE = /\.(png|jpe?g|webp|gif|bmp|tiff?|ico|avif|heic|pdf|docx?|pptx?|xlsx|xlsm|xls|odt|odp|ods|rtf|key|pages)$/i;

/** Which richer view a file has beyond the plain text editor ("" = none). */
export function richKind(path: string): "table" | "document" | "media" | "data" | "" {
  if (TABLE_EXT.test(path)) return "table";
  if (DOC_EXT.test(path)) return "document";
  if (MEDIA_EXT.test(path)) return "media";
  if (DATA_EXT.test(path)) return "data";
  return "";
}

export function RichPreview({ project, path, version }: { project: string; path: string; version?: number }) {
  const [data, setData] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const [sheet, setSheet] = useState(0);
  const [pdfMode, setPdfMode] = useState<"view" | "text">("view");
  const kind = richKind(path);

  useEffect(() => {
    if (kind === "media") return;
    setData(null);
    setError("");
    setSheet(0);
    api.preview(project, path).then(setData).catch((e) => setError((e as Error).message));
  }, [project, path, kind, version]);

  if (kind === "media") {
    const src = api.rawUrl(project, path);
    return (
      <div className="flex h-full items-center justify-center p-6">
        {VIDEO_EXT.test(path) ? <video src={src} controls className="max-h-full max-w-full rounded-lg shadow-card" /> : <audio src={src} controls className="w-full max-w-xl" />}
      </div>
    );
  }
  if (error) return <div className="m-6 flex items-start gap-2 rounded-lg border border-bad/40 bg-bad/10 p-3 text-[13px] text-bad"><AlertTriangle size={15} className="mt-0.5 shrink-0" />{error}</div>;
  if (!data) return <div className="flex h-full items-center justify-center text-faint"><Loader2 size={18} className="animate-spin" /></div>;

  if (data.kind === "pdf") {
    return (
      <div className="flex h-full flex-col">
        <div className="flex shrink-0 items-center gap-2 border-b border-line/60 px-4 py-1.5 text-[12px] text-faint">
          <Seg value={pdfMode} onChange={setPdfMode} options={[["view", "Document"], ["text", "Extracted text"]]} />
          <span>{data.pages} page{data.pages === 1 ? "" : "s"}</span>
          {data.warning && <span className="truncate text-warn">{data.warning}</span>}
        </div>
        {pdfMode === "view"
          ? <iframe title={path} src={api.rawUrl(project, path)} className="min-h-0 flex-1 bg-white" />
          : <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap px-6 py-4 font-mono text-[12.5px] leading-relaxed">{data.text}</pre>}
      </div>
    );
  }

  const tables = data.tables;
  if (tables.length && (data.kind === "table" || data.kind === "database" || data.kind === "archive")) {
    const t = tables[Math.min(sheet, tables.length - 1)];
    return (
      <div className="flex h-full flex-col">
        {tables.length > 1 && (
          <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-line/60 px-3 py-1.5">
            {tables.map((x, i) => (
              <button key={x.name + i} onClick={() => setSheet(i)}
                className={clsx("flex items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 py-1 text-[12.5px]", i === sheet ? "bg-raised text-ink" : "text-muted hover:text-ink")}>
                <Table2 size={12} /> {x.name} <span className="text-faint">{x.total_rows}</span>
              </button>
            ))}
          </div>
        )}
        <TableView t={t} />
      </div>
    );
  }

  if (data.kind === "document") {
    return (
      <div className="h-full overflow-auto">
        <div className="mx-auto max-w-3xl px-6 py-5">
          {data.pages ? <div className="mb-3 text-[12px] text-faint">{data.format.toUpperCase()} · {data.pages} {data.format === "pptx" ? "slides" : "sections"}</div> : null}
          <Markdown text={data.text} />
        </div>
      </div>
    );
  }

  return (
    <pre className="h-full overflow-auto whitespace-pre-wrap px-6 py-4 font-mono text-[12.5px] leading-relaxed">
      {data.warning && <div className="mb-3 text-warn">{data.warning}</div>}
      {data.text}
    </pre>
  );
}

function TableView({ t }: { t: PreviewTable }) {
  const shown = t.rows.length;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="min-w-full border-separate border-spacing-0 text-left text-[12.5px]">
          <thead className="sticky top-0 z-10">
            <tr>
              <th className="border-b border-r border-line bg-panel px-2 py-1.5 text-right font-mono text-[11px] font-normal text-faint">#</th>
              {t.columns.map((c, i) => (
                <th key={i} className="whitespace-nowrap border-b border-r border-line bg-panel px-3 py-1.5 font-semibold">{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {t.rows.map((r, i) => (
              <tr key={i} className="hover:bg-raised/50">
                <td className="border-b border-r border-line/60 px-2 py-1 text-right font-mono text-[11px] text-faint">{i + 1}</td>
                {t.columns.map((_, j) => {
                  const v = r[j];
                  const num = typeof v === "number" || (typeof v === "string" && v !== "" && !isNaN(Number(v)));
                  return (
                    <td key={j} className={clsx("max-w-[28rem] truncate border-b border-r border-line/60 px-3 py-1", num && "text-right font-mono tabular-nums")}
                      title={v == null ? "" : String(v)}>
                      {v == null ? "" : String(v)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="shrink-0 border-t border-line px-4 py-1.5 text-[11.5px] text-faint">
        {t.total_rows} rows × {t.columns.length} columns{shown < t.total_rows ? ` · showing the first ${shown}` : ""}
      </div>
    </div>
  );
}

export function LookPanel({ project, path, pages }: { project: string; path: string; pages?: number | null }) {
  const [question, setQuestion] = useState("");
  const [page, setPage] = useState(1);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { setAnswer(""); setError(""); setPage(1); }, [path]);
  const ask = async () => {
    setBusy(true);
    setError("");
    try {
      setAnswer((await api.look(project, path, question, page)).text);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const paged = !/\.(png|jpe?g|webp|gif|bmp|tiff?|ico|avif|heic)$/i.test(path);
  return (
    <div className="shrink-0 border-t border-line bg-panel/60 px-4 py-3">
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void ask(); }}>
        {paged && (
          <input type="number" min={1} max={pages ?? undefined} value={page} onChange={(e) => setPage(Math.max(1, Number(e.target.value)))}
            className="input w-20" title="page" />
        )}
        <input className="input" value={question} onChange={(e) => setQuestion(e.target.value)}
          placeholder={paged ? "Ask the vision model about this page (charts, layout, scanned text…)" : "Ask the vision model about this image (or leave empty to describe it)"} />
        <button className="btn-outline shrink-0" disabled={busy}><Busy on={busy}><Eye size={14} /></Busy> Look</button>
      </form>
      {error && <p className="mt-2 text-[12.5px] text-bad">{error}</p>}
      {answer && (
        <div className="mt-2 max-h-56 overflow-y-auto rounded-lg border border-line bg-bg px-3 py-2 text-[13px]">
          <div className="mb-1 flex items-center gap-1.5 text-[11px] text-faint"><FileText size={11} /> vision model</div>
          <Markdown text={answer} />
        </div>
      )}
    </div>
  );
}

function Seg<T extends string>({ value, onChange, options }: { value: T; onChange: (v: T) => void; options: [T, string][] }) {
  return (
    <div className="flex rounded-lg border border-line p-0.5">
      {options.map(([v, l]) => (
        <button key={v} onClick={() => onChange(v)} className={clsx("rounded-md px-2 py-0.5 text-[12px]", value === v ? "bg-raised text-ink" : "text-faint hover:text-ink")}>{l}</button>
      ))}
    </div>
  );
}
