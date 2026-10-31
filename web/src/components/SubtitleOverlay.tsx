// 把服务端排好的字幕（与导出的 ASS 同一套位置）叠在画面上。
// 传了 onMove 时（审校页），每条字幕都能在画面上直接拖：松手时把字幕块底边中点的位置（相对画面 0~1）交给 onMove 保存。
// 拖的时候靠近画面正中、四边的安全边会吸附（按住 Alt 不吸）；点一下选中后可以用方向键微调。
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from "react";
import type { Candidate, Preview, PreviewHl, PreviewItem } from "../types";
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

type Guides = { x: number[]; y: number[] };  // 吸附到的参考线（相对画面的像素）
/** 拖动 / 微调要用的：保存、选中、画参考线。没传 onMove（只看不改的地方）就没有。 */
interface Ctl { k: number; onMove: OnMoveSub; picked: string | null; pick: (cue: string | null) => void; setGuides: (g: Guides | null) => void }

export function SubtitleOverlay({ preview, time, width, height, boxes, onMove, onPick, hl }: {
  preview: Preview | null; time: number; width: number; height: number; boxes?: Candidate[]; onMove?: OnMoveSub;
  onPick?: (cueId: string) => void;  // 在画面上点选了一条字幕（审校页跟着选中这一条）
  hl?: PreviewHl;
}) {
  const [picked, setPicked] = useState<string | null>(null);
  const [guides, setGuides] = useState<Guides | null>(null);
  useEffect(() => {  // 点到字幕以外的地方、按 Esc：取消选中
    if (!picked) return;
    const down = (e: MouseEvent) => { if (!(e.target as HTMLElement)?.closest?.(".subs .drag")) setPicked(null); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setPicked(null); };
    window.addEventListener("mousedown", down);
    window.addEventListener("keydown", key);
    return () => { window.removeEventListener("mousedown", down); window.removeEventListener("keydown", key); };
  }, [picked]);
  if (!preview || !height) return null;
  const k = height / preview.py;
  // 视频画面按比例居中显示：字幕坐标系（px × py）映射到实际画面区域
  const vw = preview.px * k;
  const ox = (width - vw) / 2;
  const items = active(preview.items, time);
  const ctl: Ctl | undefined = onMove && {
    k, onMove, picked, setGuides, pick: (cue) => { setPicked(cue); if (cue) onPick?.(cue); },
  };
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
        ? <Info key={`${it.cue ?? i}-${it.start}`} it={it} preview={preview} k={k} ctl={ctl} hl={!!hl?.style && it.style === hl.style} />
        : <Main key={`${it.cue ?? i}-${it.start}`} it={it} preview={preview} k={k} ctl={ctl} hl={hl} />))}
      {guides?.x.map((x) => <div key={`x${x}`} className="guide v" style={{ left: x }} />)}
      {guides?.y.map((y) => <div key={`y${y}`} className="guide h" style={{ top: y }} />)}
    </div>
  );
}

export const DROP: Record<string, string> = { logo: "台标", junk: "杂讯", scene: "实物上的字", ruby: "振假名" };

const SNAP = 8;     // 离参考线这么近（屏幕像素）就吸过去
const MARGIN = 46;  // 四边的安全边（1080 画布上的像素，和自动排版时台词离底边的距离一样）

/** 字幕块里字实际占的范围（相对画面）：台词块本身是整行宽的，要看里面每一行。 */
function bounds(el: HTMLElement, sub: HTMLElement) {
  const s = sub.getBoundingClientRect();
  const rs = el.classList.contains("blk") && el.children.length
    ? [...el.children].map((c) => c.getBoundingClientRect()) : [el.getBoundingClientRect()];
  const l = Math.min(...rs.map((r) => r.left)) - s.left, t = Math.min(...rs.map((r) => r.top)) - s.top;
  const r = Math.max(...rs.map((x) => x.right)) - s.left, b = Math.max(...rs.map((x) => x.bottom)) - s.top;
  return { l, t, r, b, W: s.width, H: s.height };
}

/** 吸附：左右看块的中心 / 左边 / 右边对齐画面正中、左右安全边，上下看底边 / 上边 / 中心对齐底部、顶部安全边、画面正中。
 *  返回要再挪的距离和吸到的参考线。 */
function snap(b: ReturnType<typeof bounds>, m: number) {
  const pick = (pairs: [number, number][]) => {
    const best = pairs.map(([at, to]) => ({ d: to - at, to })).filter((p) => Math.abs(p.d) <= SNAP)
      .sort((a, c) => Math.abs(a.d) - Math.abs(c.d))[0];
    return best ?? { d: 0, to: null };
  };
  const x = pick([[(b.l + b.r) / 2, b.W / 2], [b.l, m], [b.r, b.W - m]]);
  const y = pick([[b.b, b.H - m], [b.t, m], [(b.t + b.b) / 2, b.H / 2]]);
  return { dx: x.d, dy: y.d, guides: { x: x.to == null ? [] : [x.to], y: y.to == null ? [] : [y.to] } };
}

/** 拖动字幕：拖的时候只是挪着看，松手才保存；保存后新位置回来了再去掉临时偏移，免得闪回原处。
 *  点一下（不拖）是选中：选中的可以用方向键微调（Shift 一次 10 像素），停手一会儿再保存。 */
function useDrag(it: PreviewItem, ctl?: Ctl) {
  const [off, setOffState] = useState<[number, number] | null>(null);
  const offRef = useRef(off);  // 连按方向键时不等重新渲染，每一下都在上一下的基础上挪
  const setOff = (o: [number, number] | null) => { offRef.current = o; setOffState(o); };
  const el = useRef<HTMLDivElement>(null);
  const ctlRef = useRef(ctl);
  ctlRef.current = ctl;
  const savedAt = useRef(0);
  // 位置从服务端回来了：刚保存过的，保持在画面上现在的位置（回来的可能是更早一次保存的结果，之后又拖过 / 微调过）；
  // 不是自己刚保存的（恢复自动、撤销），去掉临时偏移
  const base = `${it.x},${it.y},${it.margin_v},${it.an},${it.top}`;
  const seen = useRef<{ base: string; at: { c: number; b: number } | null }>({ base, at: null });
  useLayoutEffect(() => {
    const node = el.current, sub = node?.closest(".subs") as HTMLElement | null;
    const b = node && sub ? bounds(node, sub) : null;
    const cur = b && { c: (b.l + b.r) / 2, b: b.b };
    const prev = seen.current;
    if (prev.base !== base) {
      const o = offRef.current;
      if (o && prev.at && cur && Date.now() - savedAt.current < 5000) {
        const n: [number, number] = [o[0] + prev.at.c - cur.c, o[1] + prev.at.b - cur.b];
        setOff(Math.abs(n[0]) < 0.5 && Math.abs(n[1]) < 0.5 ? null : n);
        seen.current = { base, at: prev.at };
        return;
      }
      if (o) setOff(null);
    }
    seen.current = { base, at: cur };
  });
  const save = () => {
    const node = el.current, sub = node?.closest(".subs") as HTMLElement | null;
    if (!node || !sub || !ctlRef.current) return;
    const b = bounds(node, sub);
    const clamp = (v: number) => Math.min(1, Math.max(0, Math.round(v * 1000) / 1000));
    savedAt.current = Date.now();
    ctlRef.current.onMove(it.cue!, [clamp((b.l + b.r) / 2 / b.W), clamp(b.b / b.H)]);
  };
  const picked = !!ctl && !!it.cue && ctl.picked === it.cue;
  useEffect(() => {
    if (!picked) return;
    let timer: number | undefined;
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.closest?.("input,textarea,select")) return;
      const dir = ({ ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] } as Record<string, number[]>)[e.key];
      if (!dir) return;
      e.preventDefault();
      const n = e.shiftKey ? 10 : 1;
      const o = offRef.current ?? [0, 0];
      setOff([o[0] + dir[0] * n, o[1] + dir[1] * n]);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => { timer = undefined; save(); }, 600);
    };
    // 捕获阶段先拿到：审校页的方向键（上下条、快退快进）看到已经处理过了就不再动
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      if (timer !== undefined) { window.clearTimeout(timer); save(); }  // 取消选中时还没保存的微调马上保存
    };
  }, [picked]);
  if (!ctl || !it.cue) return { off, props: {} };
  const onMouseDown = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const node = e.currentTarget, sub = node.closest(".subs") as HTMLElement;
    const x0 = e.clientX, y0 = e.clientY;
    const start = offRef.current ?? [0, 0];
    const b0 = bounds(node, sub);
    const m = MARGIN * ctl.k;
    let moved = false;
    const move = (ev: MouseEvent) => {
      const dx = ev.clientX - x0, dy = ev.clientY - y0;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      if (!moved) return;
      const s = ev.altKey ? { dx: 0, dy: 0, guides: null }
        : snap({ ...b0, l: b0.l + dx, r: b0.r + dx, t: b0.t + dy, b: b0.b + dy }, m);
      setOff([start[0] + dx + s.dx, start[1] + dy + s.dy]);
      ctl.setGuides(s.guides && (s.guides.x.length || s.guides.y.length) ? s.guides : null);
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      ctl.setGuides(null);
      ctl.pick(it.cue!);
      if (moved) save();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  const tip = "拖动改位置：靠近画面正中、四边会吸附（按住 Alt 不吸附）；点一下选中后可以用方向键微调（Shift 一次 10 像素）";
  return {
    off,
    props: { ref: el, onMouseDown, className: `drag${picked ? " picked" : ""}`, title: it.moved ? `${tip}。已手动放过，右栏可以恢复自动` : tip },
  };
}

const shift = (off: [number, number] | null) => (off ? ` translate(${off[0]}px, ${off[1]}px)` : "");

function Main({ it, preview, k, ctl, hl }: { it: PreviewItem; preview: Preview; k: number; ctl?: Ctl; hl?: PreviewHl }) {
  const { off, props } = useDrag(it, ctl);
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
          <div key={i} className={`ly ${it.type === "song" ? "song" : ""}${(hl?.layer != null && r.layer === hl.layer) || (hl?.style && r.style === hl.style) ? " hl" : ""}`}
               style={{ ...css, height: r.height * k }}>
            {r.chunks.map(([t, ruby], j) => (ruby ? <ruby key={j}>{t}<rt style={rt}>{ruby}</rt></ruby> : <span key={j}>{t}</span>))}
          </div>
        );
      })}
    </div>
  );
}

function Info({ it, preview, k, ctl, hl }: { it: PreviewItem; preview: Preview; k: number; ctl?: Ctl; hl?: boolean }) {
  const { off, props } = useDrag(it, ctl);
  const st = preview.styles[it.style!] ?? Object.values(preview.styles)[0];
  const css: CSSProperties = { ...styleCss(st, k) };
  if (it.top) {
    return (
      <div {...props} className={`blk ${props.className ?? ""}`} style={{ top: "4%", ...(off ? { transform: shift(off).trim() } : {}) }}>
        <div className={`ly${hl ? " hl" : ""}`} style={css}>{it.lines!.join("\n")}</div>
      </div>
    );
  }
  const an = it.an ?? 2;
  const tx = an % 3 === 1 ? "0" : an % 3 === 2 ? "-50%" : "-100%";
  const ty = an <= 3 ? "-100%" : an <= 6 ? "-50%" : "0";
  return (
    <div {...props} className={`near ${props.className ?? ""}${hl ? " hl" : ""}`}
         style={{ ...css, left: it.x! * k, top: it.y! * k, transform: `translate(${tx}, ${ty})${shift(off)}`, textAlign: an % 3 === 0 ? "right" : an % 3 === 2 ? "center" : "left" }}>
      {it.lines!.join("\n")}
    </div>
  );
}
