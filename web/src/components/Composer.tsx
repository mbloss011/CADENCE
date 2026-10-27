import clsx from "clsx";
import { ArrowUp, CornerDownRight, ImagePlus, Loader2, Square, X } from "lucide-react";
import { useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent } from "react";
import { api } from "../lib/api";
import { useStore } from "../lib/store";
import { Kbd } from "./ui";

type Upload = { id: string; preview: string; name: string } | { pending: true; preview: string; name: string };

export function Composer({
  running,
  disabled,
  onSend,
  onStop,
}: {
  running: boolean;
  disabled?: boolean;
  onSend: (text: string, uploads: { id: string; preview: string }[]) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const store = useStore();

  useEffect(() => {
    if (store.draft === null) return;
    setText(store.draft);
    store.setDraft(null);
    requestAnimationFrame(() => {
      const el = ref.current;
      if (el) {
        el.focus();
        el.selectionStart = el.selectionEnd = el.value.length;
      }
    });
  }, [store.draft]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "";
    if (!text) return; // empty: natural one-row height (measuring before layout settles over-sizes it)
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [text]);

  useEffect(() => {
    ref.current?.focus();
  }, [running]);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape" && running) {
        e.preventDefault();
        onStop();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [running, onStop]);

  const addFiles = async (files: File[]) => {
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (!images.length) return;
    setError("");
    for (const f of images) {
      const preview = URL.createObjectURL(f);
      setUploads((u) => [...u, { pending: true, preview, name: f.name }]);
      try {
        const res = await api.upload(f);
        setUploads((u) => u.map((x) => (x.preview === preview ? { id: res.id, preview, name: f.name } : x)));
      } catch (e) {
        setUploads((u) => u.filter((x) => x.preview !== preview));
        setError(`Upload failed: ${(e as Error).message}`);
      }
    }
  };

  const ready = uploads.filter((u): u is Extract<Upload, { id: string }> => "id" in u);
  const busy = uploads.some((u) => "pending" in u);
  const canSend = !disabled && !busy && (text.trim().length > 0 || (!running && ready.length > 0));

  const submit = () => {
    if (!canSend) return;
    onSend(text.trim(), running ? [] : ready.map(({ id, preview }) => ({ id, preview })));
    setText("");
    if (!running) setUploads([]);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const onPaste = (e: ClipboardEvent) => {
    const files = Array.from(e.clipboardData.files);
    if (files.length) {
      e.preventDefault();
      void addFiles(files);
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDrag(false);
    void addFiles(Array.from(e.dataTransfer.files));
  };

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-4">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={onDrop}
        className={clsx(
          "rounded-2xl border bg-panel shadow-card transition",
          drag ? "border-brand ring-2 ring-brand/30" : running ? "border-brand/50" : "border-line focus-within:border-faint",
        )}
      >
        {running && (
          <div className="flex items-center gap-2 border-b border-line/70 px-4 py-1.5 text-[12px] text-brand">
            <CornerDownRight size={13} />
            <span>Agent is working — messages you send now steer it before its next step.</span>
          </div>
        )}
        {uploads.length > 0 && (
          <div className="flex flex-wrap gap-2 px-3 pt-3">
            {uploads.map((u) => (
              <div key={u.preview} className="group relative">
                <img src={u.preview} alt={u.name} className={clsx("h-16 w-16 rounded-lg border border-line object-cover", "pending" in u && "opacity-50")} />
                {"pending" in u ? (
                  <Loader2 size={16} className="absolute inset-0 m-auto animate-spin text-ink" />
                ) : (
                  <button
                    onClick={() => setUploads((x) => x.filter((y) => y.preview !== u.preview))}
                    className="absolute -right-1.5 -top-1.5 hidden rounded-full border border-line bg-panel p-0.5 text-muted hover:text-ink group-hover:block"
                    aria-label="remove image"
                  >
                    <X size={12} />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
        <textarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          rows={1}
          placeholder={running ? "Steer: e.g. “skip the tests for now, focus on the parser”" : "Ask Kestrel to build, fix, or explain something…"}
          className="block max-h-60 w-full resize-none bg-transparent px-4 pb-1 pt-3 text-[14.5px] leading-relaxed placeholder:text-faint focus:outline-none"
          disabled={disabled}
        />
        <div className="flex items-center gap-2 px-2.5 pb-2.5 pt-1">
          <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden
            onChange={(e) => {
              void addFiles(Array.from(e.target.files ?? []));
              e.target.value = "";
            }}
          />
          <button onClick={() => fileRef.current?.click()} disabled={running} className="btn-ghost px-2" title="Attach image (or paste / drop)">
            <ImagePlus size={17} />
          </button>
          <div className="flex-1 truncate text-[11px] text-faint">
            {error ? <span className="text-bad">{error}</span> : running ? (
              <span><Kbd>Enter</Kbd> steer · <Kbd>Esc</Kbd> stop</span>
            ) : (
              <span className="hidden sm:inline"><Kbd>Enter</Kbd> send · <Kbd>Shift</Kbd>+<Kbd>Enter</Kbd> newline · paste or drop images</span>
            )}
          </div>
          {running && (
            <button onClick={onStop} className="btn-danger px-2.5" title="Interrupt (Esc)">
              <Square size={13} fill="currentColor" /> Stop
            </button>
          )}
          <button onClick={submit} disabled={!canSend} className="btn-primary h-8 px-3" title={running ? "Steer" : "Send"}>
            {running ? <><CornerDownRight size={15} /> Steer</> : <ArrowUp size={17} strokeWidth={2.5} />}
          </button>
        </div>
      </div>
    </div>
  );
}
