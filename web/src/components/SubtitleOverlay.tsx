// 把服务端排好的字幕（与导出的 ASS 同一套位置）叠在画面上。
// 传了 onMove 时（审校页），每条字幕都能在画面上直接拖：松手时把字幕块底边中点的位置（相对画面 0~1）交给 onMove 保存。
import { useEffect, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from "react";
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

export type OnMoveSub = (cueId: string, pos: [number, number]) => void;

export function SubtitleOverlay({ preview, time, width, height, boxes, onMove }: {
  preview: Preview | null; time: number; width: number; height: number; boxes?: Candidate[]; onMove?: OnMoveSub;
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
      {items.map((it, i) => (it.kind === "info"
        ? <Info key={`${it.cue ?? i}-${it.start}`} it={it} preview={preview} k={k} onMove={onMove} />
        : <Main key={`${it.cue ?? i}-${it.start}`} it={it} preview={preview} k={k} onMove={onMove} />))}
    </div>
  );
}

const DROP: Record<string, string> = { logo: "台标", junk: "杂讯", scene: "实物上的字" };

/** 拖动字幕：拖的时候只是挪着看，松手才保存；保存后新位置回来了再去掉临时偏移，免得闪回原处。 */
function useDrag(it: PreviewItem, onMove?: OnMoveSub) {
  const [off, setOff] = useState<[number, number] | null>(null);
  useEffect(() => { setOff(null); }, [it.x, it.y, it.margin_v, it.an, it.top]);
  if (!onMove || !it.cue) return { off, props: {} };
  const onMouseDown = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const el = e.currentTarget, sub = el.closest(".subs") as HTMLElement;
    const x0 = e.clientX, y0 = e.clientY;
    let moved = false;
    const move = (ev: MouseEvent) => {
      const d: [number, number] = [ev.clientX - x0, ev.clientY - y0];
      if (Math.abs(d[0]) + Math.abs(d[1]) > 3) moved = true;
      setOff(d);
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      if (!moved) { setOff(null); return; }
      const r = el.getBoundingClientRect(), s = sub.getBoundingClientRect();
      const clamp = (v: number) => Math.min(1, Math.max(0, Math.round(v * 1000) / 1000));
      onMove(it.cue!, [clamp((r.left + r.width / 2 - s.left) / s.width), clamp((r.bottom - s.top) / s.height)]);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  return {
    off,
    props: { onMouseDown, className: "drag", title: it.moved ? "拖动改位置（已手动放过；右栏可以恢复自动）" : "拖动可以改这条字幕的位置" },
  };
}

const shift = (off: [number, number] | null) => (off ? ` translate(${off[0]}px, ${off[1]}px)` : "");

function Main({ it, preview, k, onMove }: { it: PreviewItem; preview: Preview; k: number; onMove?: OnMoveSub }) {
  const { off, props } = useDrag(it, onMove);
  // 手动放过位置：底边中点在 (x, y)；否则居中，离底边 margin_v
  const at: CSSProperties = it.x != null && it.y != null
    ? { left: it.x * k, top: it.y * k, right: "auto", transform: `translate(-50%, -100%)${shift(off)}` }
    : { bottom: (it.margin_v ?? 46) * k, ...(off ? { transform: shift(off).trim() } : {}) };
  return (
    <div {...props} className={`blk ${props.className ?? ""}`} style={at}>
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

function Info({ it, preview, k, onMove }: { it: PreviewItem; preview: Preview; k: number; onMove?: OnMoveSub }) {
  const { off, props } = useDrag(it, onMove);
  const st = preview.styles[it.style!] ?? Object.values(preview.styles)[0];
  const css: CSSProperties = { ...styleCss(st, k) };
  if (it.top) {
    return (
      <div {...props} className={`blk ${props.className ?? ""}`} style={{ top: "4%", ...(off ? { transform: shift(off).trim() } : {}) }}>
        <div className="ly" style={css}>{it.lines!.join("\n")}</div>
      </div>
    );
  }
  const an = it.an ?? 2;
  const tx = an % 3 === 1 ? "0" : an % 3 === 2 ? "-50%" : "-100%";
  const ty = an <= 3 ? "-100%" : an <= 6 ? "-50%" : "0";
  return (
    <div {...props} className={`near ${props.className ?? ""}`}
         style={{ ...css, left: it.x! * k, top: it.y! * k, transform: `translate(${tx}, ${ty})${shift(off)}`, textAlign: an % 3 === 0 ? "right" : an % 3 === 2 ? "center" : "left" }}>
      {it.lines!.join("\n")}
    </div>
  );
}
