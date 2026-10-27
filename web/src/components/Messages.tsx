import clsx from "clsx";
import { AlertTriangle, Brain, ChevronRight, CornerDownRight, Info, OctagonX } from "lucide-react";
import { memo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Item } from "../lib/api";
import { ToolCard } from "./ToolCard";
import { Spinner } from "./ui";

const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
});

function Thinking({ text, streaming }: { text: string; streaming?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="animate-rise">
      <button onClick={() => setOpen((o) => !o)} className="flex items-center gap-1.5 text-xs text-faint hover:text-muted">
        <ChevronRight size={12} className={clsx("transition", open && "rotate-90")} />
        <Brain size={13} />
        {streaming ? (
          <span className="flex items-center gap-1.5">
            Thinking <Spinner />
          </span>
        ) : (
          <span>Thought for {Math.max(1, Math.round(text.length / 4))} tokens</span>
        )}
      </button>
      {(open || streaming) && (
        <div
          className={clsx(
            "ml-[18px] mt-1.5 whitespace-pre-wrap border-l-2 border-line pl-3 text-[13px] italic leading-relaxed text-faint",
            !open && "line-clamp-3",
          )}
        >
          {open ? text : text.slice(-400)}
        </div>
      )}
    </div>
  );
}

export function MessageItem({ item }: { item: Item }) {
  switch (item.kind) {
    case "user":
      return (
        <div className="flex justify-end animate-rise">
          <div className="max-w-[85%] space-y-2">
            {item.images.length > 0 && (
              <div className="flex flex-wrap justify-end gap-2">
                {item.images.map((src, i) => (
                  <a key={i} href={src} target="_blank" rel="noreferrer">
                    <img src={src} alt="attachment" className="h-28 rounded-lg border border-line object-cover" />
                  </a>
                ))}
              </div>
            )}
            {item.text && (
              <div className="whitespace-pre-wrap rounded-2xl rounded-br-md bg-raised px-4 py-2.5 text-[14.5px] leading-relaxed">
                {item.text}
              </div>
            )}
          </div>
        </div>
      );
    case "steer":
      return (
        <div className="flex justify-end animate-rise">
          <div className="flex max-w-[85%] items-start gap-2 rounded-xl border border-brand/40 bg-brand/[0.07] px-3 py-2 text-sm">
            <CornerDownRight size={14} className="mt-0.5 shrink-0 text-brand" />
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-wider text-brand">steering</div>
              <div className="whitespace-pre-wrap text-ink">{item.text}</div>
            </div>
          </div>
        </div>
      );
    case "assistant":
      return (
        <div className="flex gap-3 animate-rise">
          <div className="min-w-0 flex-1 text-[14.5px] leading-relaxed">
            <Markdown text={item.text} />
            {item.streaming && <span className="ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 animate-pulse bg-brand" />}
          </div>
        </div>
      );
    case "thinking":
      return <Thinking text={item.text} streaming={item.streaming} />;
    case "tool":
      return <ToolCard name={item.name} arguments={item.arguments} result={item.result} />;
    case "note": {
      const Icon = item.tone === "bad" ? OctagonX : item.tone === "warn" ? AlertTriangle : Info;
      return (
        <div
          className={clsx("flex items-start gap-2 rounded-lg px-3 py-2 text-[13px] animate-rise", {
            "bg-bad/10 text-bad": item.tone === "bad",
            "bg-warn/10 text-warn": item.tone === "warn",
            "bg-info/10 text-info": item.tone === "info",
          })}
        >
          <Icon size={14} className="mt-0.5 shrink-0" />
          <span className="whitespace-pre-wrap">{item.text}</span>
        </div>
      );
    }
  }
}
