// 在样帧上拖动框选画面字的识别区（绿）和排除区（红，台标、固定角标）。
import { useRef, useState } from "react";
import { api } from "../api";
import type { Candidate } from "../types";
import { fmt } from "../util";

export interface Region { rect: [number, number, number, number]; exclude?: boolean; name?: string }

export function RegionEditor({ pid, duration, regions, boxes, onChange }: {
  pid: string; duration: number; regions: Region[]; boxes: Candidate[]; onChange: (r: Region[]) => void;
}) {
  const [t, setT] = useState(() => Math.round(Math.min(duration, Math.max(1, duration * 0.1)) * 10) / 10);
  const [mode, setMode] = useState<"in" | "ex">("in");
  const [drag, setDrag] = useState<Region | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const start = useRef<[number, number]>([0, 0]);

  const pos = (e: React.MouseEvent): [number, number] => {
    const r = box.current!.getBoundingClientRect();
    return [Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)), Math.max(0, Math.min(1, (e.clientY - r.top) / r.height))];
  };
  const round = (x: number) => Math.round(x * 1000) / 1000;
  const shown = drag ? [...regions, drag] : regions;
  const active = boxes.filter((b) => b.rect && t >= b.start && t < b.end);

  return (
    <div>
      <div className="row" style={{ marginBottom: 6 }}>
        <span className="lbl">识别区域</span><span className="sp" />
        <div className="seg">
          <button className={mode === "in" ? "on" : ""} onClick={() => setMode("in")}>＋ 识别区</button>
          <button className={mode === "ex" ? "on" : ""} onClick={() => setMode("ex")}>＋ 排除区</button>
        </div>
        <button className="btn sm ghost" onClick={() => onChange([{ rect: [0, 0, 1, 1] }])}>重置</button>
      </div>
      <div ref={box} className="frame" style={{ cursor: "crosshair" }}
           onMouseDown={(e) => { start.current = pos(e); const [x, y] = start.current; setDrag({ rect: [x, y, x, y], exclude: mode === "ex" }); e.preventDefault(); }}
           onMouseMove={(e) => {
             if (!drag) return;
             const [x0, y0] = start.current, [x1, y1] = pos(e);
             setDrag({ ...drag, rect: [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)] });
           }}
           onMouseUp={() => {
             if (drag && drag.rect[2] - drag.rect[0] > 0.02 && drag.rect[3] - drag.rect[1] > 0.02) {
               // 新画识别区时，去掉默认的“整个画面”识别区
               const base = !drag.exclude ? regions.filter((r) => r.exclude || r.rect.join() !== "0,0,1,1") : regions;
               onChange([...base, { ...drag, rect: drag.rect.map(round) as Region["rect"] }]);
             }
             setDrag(null);
           }}
           onMouseLeave={() => setDrag(null)}>
        <img src={api.frameUrl(pid, t)} alt="" draggable={false}
             style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "contain" }} />
        {active.map((b) => {
          const [x0, y0, x1, y1] = b.rect!;
          return <div key={b.id} title={b.text} style={{ position: "absolute", left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`, border: "1px dashed #ffffffaa" }} />;
        })}
        {shown.map((g, i) => {
          const [x0, y0, x1, y1] = g.rect;
          return (
            <div key={i} style={{ position: "absolute", left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
                                  border: `2px solid ${g.exclude ? "var(--bad)" : "var(--ok)"}`, background: g.exclude ? "#ff6b6b26" : "#5fd39a14", pointerEvents: "none" }}>
              <span style={{ position: "absolute", left: 2, top: 1, fontSize: 10, background: "#0009", padding: "0 4px", borderRadius: 2 }}>{g.exclude ? "排除" : "识别"}{g.name ? `·${g.name}` : ""}</span>
            </div>
          );
        })}
      </div>
      <div className="row" style={{ margin: "6px 0" }}>
        <span className="dim" style={{ fontSize: 12 }}>样帧</span>
        <input type="range" min={0} max={Math.max(1, duration)} step={0.5} value={t} onChange={(e) => setT(Number(e.target.value))} />
        <span className="dim" style={{ width: 48 }}>{fmt(t).slice(0, 5)}</span>
      </div>
      <div className="dim" style={{ fontSize: 11.5, marginBottom: 6 }}>在画面上拖动框选；虚线框是这一时刻已经识别到的文字</div>
      {regions.map((g, i) => (
        <div key={i} className="row" style={{ fontSize: 12, padding: "2px 0" }}>
          <span className={`chip ${g.exclude ? "bad" : "ok"}`}>{g.exclude ? "排除" : "识别"}</span>
          <span className="dim">{g.rect.map((x) => x.toFixed(2)).join(", ")}</span>
          <input className="inp" style={{ width: 90, padding: "1px 6px" }} placeholder="名字" value={g.name ?? ""}
                 onChange={(e) => onChange(regions.map((r, k) => (k === i ? { ...r, name: e.target.value } : r)))} />
          <span className="sp" />
          <button className="btn sm ghost" onClick={() => onChange(regions.filter((_, k) => k !== i))}>删除</button>
        </div>
      ))}
    </div>
  );
}
