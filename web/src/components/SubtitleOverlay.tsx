// 把服务端排好的字幕（与导出的 ASS 同一套位置）叠在画面上。
import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { Candidate, Preview, PreviewItem } from "../types";
import { styleCss } from "../util";

export function useSize<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setSize({ w: e.contentRect.width, h: e.contentRect.height }));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, size] as const;
}

const active = <T extends { start: number; end: number }>(xs: T[], t: number) => xs.filter((x) => t >= x.start && t < x.end);

export function SubtitleOverlay({ preview, time, width, height, boxes }: {
  preview: Preview | null; time: number; width: number; height: number; boxes?: Candidate[];
}) {
  if (!preview || !height) return null;
  const k = height / preview.py;
  // 视频画面按比例居中显示：字幕坐标系（px × py）映射到实际画面区域
  const vw = preview.px * k;
  const ox = (width - vw) / 2;
  const items = active(preview.items, time);
  return (
    <div className="subs" style={{ left: ox, width: vw }}>
      {boxes && active(boxes, time).filter((b) => b.rect).map((b) => {
        const [x0, y0, x1, y1] = b.rect!;
        return (
          <div key={b.id} className={`bx ${b.dropped ? "drop" : ""}`}
               style={{ left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` }}>
            <i>{b.id} {b.dropped ? `丢弃：${DROP[b.dropped] ?? b.dropped}` : b.dup_of ? "与字幕重复" : `${Math.round((b.score ?? 1) * 100)}%`}</i>
          </div>
        );
      })}
      {items.map((it, i) => (it.kind === "info" ? <Info key={i} it={it} preview={preview} k={k} /> : <Main key={i} it={it} preview={preview} k={k} />))}
    </div>
  );
}

const DROP: Record<string, string> = { logo: "台标", junk: "杂讯", scene: "实物上的字" };

function Main({ it, preview, k }: { it: PreviewItem; preview: Preview; k: number }) {
  return (
    <div className="blk" style={{ bottom: (it.margin_v ?? 46) * k }}>
      {it.rows!.map((r, i) => {
        const st = preview.styles[r.style] ?? Object.values(preview.styles)[0];
        const css = styleCss(st, k);
        const rt: CSSProperties = { fontSize: `${r.rsize / st.size}em`, ...(r.rcolor ? { color: r.rcolor } : {}) };
        return (
          <div key={i} className={`ly ${it.type === "song" ? "song" : ""}`} style={{ ...css, height: r.height * k }}>
            {r.chunks.map(([t, ruby], j) => (ruby ? <ruby key={j}>{t}<rt style={rt}>{ruby}</rt></ruby> : <span key={j}>{t}</span>))}
          </div>
        );
      })}
    </div>
  );
}

function Info({ it, preview, k }: { it: PreviewItem; preview: Preview; k: number }) {
  const st = preview.styles[it.style!] ?? Object.values(preview.styles)[0];
  const css: CSSProperties = { ...styleCss(st, k) };
  if (it.top) {
    return <div className="blk" style={{ top: "4%" }}><div className="ly" style={css}>{it.lines!.join("\n")}</div></div>;
  }
  const an = it.an ?? 2;
  const tx = an % 3 === 1 ? "0" : an % 3 === 2 ? "-50%" : "-100%";
  const ty = an <= 3 ? "-100%" : an <= 6 ? "-50%" : "0";
  return (
    <div className="near" style={{ ...css, left: it.x! * k, top: it.y! * k, transform: `translate(${tx}, ${ty})`, textAlign: an % 3 === 0 ? "right" : an % 3 === 2 ? "center" : "left" }}>
      {it.lines!.join("\n")}
    </div>
  );
}
