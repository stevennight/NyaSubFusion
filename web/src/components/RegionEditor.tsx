// 画面字的识别区（绿）和排除区（红，台标、固定角标）。
// 流程页上是一张小预览，点开是大的编辑窗口：拖动画新框、拖框内移动、拖四角改大小、直接输入坐标；
// 样帧可以逐秒 / 逐条画面字翻看，虚线框是那一刻已经识别到的文字。
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import type { Candidate } from "../types";
import { fmt } from "../util";

export interface Region { rect: [number, number, number, number]; exclude?: boolean; name?: string }
type Rect = Region["rect"];
type Drag =
  | { kind: "new"; x: number; y: number; exclude: boolean; pick?: number }  // pick：按在了哪个框上（只点一下 = 选中它）
  | { kind: "move"; i: number; x: number; y: number; orig: Rect }
  | { kind: "resize"; i: number; corner: number; orig: Rect };

const clamp = (v: number) => Math.max(0, Math.min(1, v));
const round = (x: number) => Math.round(x * 1000) / 1000;
const norm = (r: Rect): Rect => [Math.min(r[0], r[2]), Math.min(r[1], r[3]), Math.max(r[0], r[2]), Math.max(r[1], r[3])].map(round) as Rect;
const isFull = (r: Region) => !r.exclude && r.rect.join() === "0,0,1,1";

function Boxes({ regions, sel, onPick, onHandle }: {
  regions: Region[]; sel?: number | null; onPick?: (i: number, e: React.MouseEvent) => void; onHandle?: (i: number, corner: number, e: React.MouseEvent) => void;
}) {
  return <>{regions.map((g, i) => {
    const [x0, y0, x1, y1] = g.rect;
    const on = sel === i;
    return (
      <div key={i} className={`rgn ${g.exclude ? "ex" : "in"} ${on ? "sel" : ""}`}
           style={{ left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
                    pointerEvents: onPick ? "auto" : "none", cursor: onPick && on && !isFull(g) ? "move" : onPick ? "crosshair" : undefined }}
           onMouseDown={onPick ? (e) => onPick(i, e) : undefined}>
        <span className="tag">{g.exclude ? "排除" : "识别"}{g.name ? `·${g.name}` : ""}</span>
        {on && onHandle && !isFull(g) && [0, 1, 2, 3].map((c) => (
          <i key={c} className={`hd h${c}`} onMouseDown={(e) => onHandle(i, c, e)} />
        ))}
      </div>
    );
  })}</>;
}

/** 流程页上的小预览：点开编辑。 */
export function RegionEditor({ pid, duration, aspect, regions, boxes, onChange }: {
  pid: string; duration: number; aspect?: number; regions: Region[]; boxes: Candidate[]; onChange: (r: Region[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const t = Math.round(Math.min(duration, Math.max(1, duration * 0.1)) * 10) / 10;
  const n = regions.filter((r) => !isFull(r)).length;
  return (
    <div>
      <div className="row" style={{ marginBottom: 6 }}>
        <span className="lbl">识别区域</span>
        <span className="dim" style={{ fontSize: 12 }}>{n ? `${regions.filter((r) => !r.exclude && !isFull(r)).length} 个识别区 · ${regions.filter((r) => r.exclude).length} 个排除区` : "整个画面"}</span>
        <span className="sp" />
        <button className="btn sm" onClick={() => setOpen(true)}>编辑识别区域…</button>
      </div>
      <div className="frame rgn-thumb" style={{ aspectRatio: String(aspect || 16 / 9), cursor: "pointer" }} onClick={() => setOpen(true)} title="点击编辑">
        <img src={api.frameUrl(pid, t)} alt="" draggable={false} className="fimg" />
        <Boxes regions={regions} />
      </div>
      {/* 挂到 body 上：不受所在卡片的样式影响，铺满整个窗口 */}
      {open && createPortal(<RegionDialog pid={pid} duration={duration} aspect={aspect || 16 / 9} regions={regions} boxes={boxes}
                             onChange={onChange} onClose={() => setOpen(false)} />, document.body)}
    </div>
  );
}

function RegionDialog({ pid, duration, aspect, regions, boxes, onChange, onClose }: {
  pid: string; duration: number; aspect: number; regions: Region[]; boxes: Candidate[]; onChange: (r: Region[]) => void; onClose: () => void;
}) {
  const [t, setT] = useState(() => Math.round(Math.min(duration, Math.max(1, duration * 0.1)) * 10) / 10);
  // 样帧：拖动进度条时每次都去取画面会排起长队（每张都要 ffmpeg 解码），停下 250 毫秒再取；
  // 新的一张加载好之前继续显示上一张
  const [img, setImg] = useState(() => api.frameUrl(pid, t, 720));
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const url = api.frameUrl(pid, t, 720);
    if (url === img) return;
    let stop = false;
    const timer = window.setTimeout(() => {
      setLoading(true);
      const pre = new Image();
      pre.onload = pre.onerror = () => { if (!stop) { setImg(url); setLoading(false); } };
      pre.src = url;
    }, 250);
    return () => { stop = true; clearTimeout(timer); };
  }, [t, pid]);
  const [mode, setMode] = useState<"in" | "ex">("in");
  const [sel, setSel] = useState<number | null>(null);
  const [, setDrag] = useState<Drag | null>(null);
  const [draft, setDraft] = useState<Rect | null>(null);
  const [hover, setHover] = useState<[number, number] | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const live = useRef(regions);
  live.current = regions;

  const pos = (e: { clientX: number; clientY: number }): [number, number] => {
    const r = box.current!.getBoundingClientRect();
    return [clamp((e.clientX - r.left) / r.width), clamp((e.clientY - r.top) / r.height)];
  };
  const setRect = (i: number, rect: Rect) => onChange(live.current.map((g, k) => (k === i ? { ...g, rect } : g)));

  // 按下鼠标时就在整个窗口上跟踪（拖出画面也不会断）；松开时按松开的位置算最终结果
  const begin = (d: Drag) => {
    setDrag(d);
    const at = (e: MouseEvent) => {
      const [x, y] = pos(e);
      if (d.kind === "new") return [Math.min(d.x, x), Math.min(d.y, y), Math.max(d.x, x), Math.max(d.y, y)] as Rect;
      if (d.kind === "move") {
        const [x0, y0, x1, y1] = d.orig;
        const dx = Math.max(-x0, Math.min(1 - x1, x - d.x)), dy = Math.max(-y0, Math.min(1 - y1, y - d.y));
        return [x0 + dx, y0 + dy, x1 + dx, y1 + dy].map(round) as Rect;
      }
      const r = [...d.orig] as Rect;
      if (d.corner === 0 || d.corner === 3) r[0] = x; else r[2] = x;
      if (d.corner === 0 || d.corner === 1) r[1] = y; else r[3] = y;
      return norm(r);
    };
    const move = (e: MouseEvent) => {
      const r = at(e);
      if (d.kind === "new") setDraft(r); else setRect(d.i, r);
    };
    const up = (e: MouseEvent) => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      const r = at(e);
      if (d.kind === "new") {
        if (r[2] - r[0] <= 0.01 && r[3] - r[1] <= 0.01) {
          if (d.pick !== undefined) setSel(d.pick);
        } else if (r[2] - r[0] > 0.01 && r[3] - r[1] > 0.01) {
          // 画第一个识别区时，去掉默认的“整个画面”
          const base = d.exclude ? live.current : live.current.filter((g) => !isFull(g));
          onChange([...base, { rect: norm(r), exclude: d.exclude }]);
          setSel(base.length);
        }
      } else setRect(d.i, r);
      setDrag(null);
      setDraft(null);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  // 键盘：Delete 删除选中的框，Esc 关闭，←→ 翻 1 秒（Shift 10 秒）
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).tagName === "INPUT") return;
      if (e.key === "Escape") onClose();
      if ((e.key === "Delete" || e.key === "Backspace") && sel !== null) { onChange(live.current.filter((_, k) => k !== sel)); setSel(null); }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); go((e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 10 : 1)); }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });

  const go = (dt: number) => setT((v) => Math.round(Math.max(0, Math.min(duration, v + dt)) * 10) / 10);
  const starts = [...new Set(boxes.filter((b) => b.rect).map((b) => Math.round(b.start * 10) / 10))].sort((a, b) => a - b);
  const jump = (dir: number) => {
    const next = dir > 0 ? starts.find((x) => x > t + 0.05) : [...starts].reverse().find((x) => x < t - 0.05);
    if (next !== undefined) setT(Math.round((next + 0.2) * 10) / 10);
  };
  const active = boxes.filter((b) => b.rect && t >= b.start && t < b.end);
  const shown = draft ? [...regions, { rect: draft, exclude: mode === "ex" } as Region] : regions;
  const pctIn = (v: number) => Math.round(v * 1000) / 10;

  return (
    <div className="modal" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal-body rgn-dialog">
        <div className="row" style={{ marginBottom: 10 }}>
          <h3 style={{ margin: 0 }}>识别区域</h3>
          <span className="dim" style={{ fontSize: 12 }}>在画面上拖动画框；拖框内移动，拖四角改大小；Delete 删除选中的框</span>
          <span className="sp" />
          <div className="seg">
            <button className={mode === "in" ? "on" : ""} onClick={() => setMode("in")}>画识别区</button>
            <button className={mode === "ex" ? "on" : ""} onClick={() => setMode("ex")}>画排除区</button>
          </div>
          <button className="btn sm ghost" onClick={() => { onChange([{ rect: [0, 0, 1, 1] }]); setSel(null); }}>重置为整个画面</button>
          <button className="btn pri" onClick={onClose}>完成</button>
        </div>
        <div className="rgn-main">
          <div className="rgn-stage">
            <div ref={box} className="frame" style={{ aspectRatio: String(aspect), cursor: "crosshair", ["--ar" as string]: aspect } as React.CSSProperties}
                 onMouseDown={(e) => { const [x, y] = pos(e); setSel(null); begin({ kind: "new", x, y, exclude: mode === "ex" }); e.preventDefault(); }}
                 onMouseMove={(e) => setHover(pos(e))} onMouseLeave={() => setHover(null)}>
              <img src={img} alt="" draggable={false} className="fimg" />
              {loading && <span className="chip" style={{ position: "absolute", left: 8, bottom: 8, background: "#000a" }}>加载中…</span>}
              {active.map((b) => {
                const [x0, y0, x1, y1] = b.rect!;
                return <div key={b.id} title={b.text} className="ocrbox"
                            style={{ left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` }} />;
              })}
              <Boxes regions={shown} sel={sel}
                     onPick={(i, e) => {
                       // 已选中的框：拖动 = 移动（“整个画面”不能移）；没选中的：拖动 = 画新框，只点一下 = 选中
                       e.stopPropagation(); e.preventDefault();
                       const [x, y] = pos(e);
                       if (sel === i && !isFull(regions[i])) begin({ kind: "move", i, x, y, orig: regions[i].rect });
                       else begin({ kind: "new", x, y, exclude: mode === "ex", pick: i });
                     }}
                     onHandle={(i, corner, e) => { e.stopPropagation(); e.preventDefault(); begin({ kind: "resize", i, corner, orig: regions[i].rect }); }} />
              {hover && <span className="rgn-xy">{pctIn(hover[0])}%, {pctIn(hover[1])}%</span>}
            </div>
            <div className="row" style={{ marginTop: 8 }}>
              <button className="btn sm" title="上一条画面字" disabled={!starts.length} onClick={() => jump(-1)}>⇤ 字</button>
              <button className="btn sm" onClick={() => go(-10)}>−10s</button>
              <button className="btn sm" onClick={() => go(-1)}>−1s</button>
              <div className="seekbar">
                <div className="ticks">{starts.map((x) => <i key={x} style={{ left: `${(x / Math.max(1, duration)) * 100}%` }} />)}</div>
                <input type="range" min={0} max={Math.max(1, duration)} step={0.1} value={t} onChange={(e) => setT(Number(e.target.value))} />
              </div>
              <button className="btn sm" onClick={() => go(1)}>+1s</button>
              <button className="btn sm" onClick={() => go(10)}>+10s</button>
              <button className="btn sm" title="下一条画面字" disabled={!starts.length} onClick={() => jump(1)}>字 ⇥</button>
              <span className="dim" style={{ width: 64, textAlign: "right" }}>{fmt(t)}</span>
            </div>
            <div className="dim" style={{ fontSize: 11.5, marginTop: 4 }}>
              进度条上的刻度是识别到画面字的位置（运行过画面字识别才有）；虚线框是这一刻识别到的文字。←→ 翻 1 秒，Shift 翻 10 秒。
            </div>
          </div>
          <div className="rgn-list">
            {regions.map((g, i) => (
              <div key={i} className={`rgn-item ${sel === i ? "sel" : ""}`} onClick={() => setSel(i)}>
                <div className="row">
                  <span className={`chip ${g.exclude ? "bad" : "ok"}`}>{g.exclude ? "排除" : "识别"}</span>
                  <input className="inp" style={{ flex: 1, padding: "2px 6px" }} placeholder="名字（可不填，如 台标）" value={g.name ?? ""}
                         onChange={(e) => onChange(regions.map((r, k) => (k === i ? { ...r, name: e.target.value } : r)))} />
                  <button className="btn sm ghost" onClick={(e) => { e.stopPropagation(); onChange(regions.filter((_, k) => k !== i)); setSel(null); }}>删除</button>
                </div>
                <div className="rgn-coords">
                  {(["左", "上", "右", "下"] as const).map((lbl, k) => (
                    <label key={k}>{lbl}
                      <input className="inp" type="number" min={0} max={100} step={0.5} value={pctIn(g.rect[k])}
                             onChange={(e) => { const r = [...g.rect] as Rect; r[k] = clamp(Number(e.target.value) / 100); setRect(i, norm(r)); }} />%
                    </label>
                  ))}
                </div>
              </div>
            ))}
            {regions.length === 0 && <div className="dim">没有区域 = 整个画面都识别</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
