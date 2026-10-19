// 多轨时间轴：波形 / 字幕 / 画面字 / 语音 三路候选 / 融合原文 / 译文。
import { useEffect, useRef, useState } from "react";
import type { Candidate, Cue, FusedItem } from "../types";
import { fmt } from "../util";

interface Props {
  duration: number; time: number; pps: number;
  subs: Candidate[]; ocr: Candidate[]; asr: Candidate[]; fused: FusedItem[]; cues: Cue[]; target: string;
  peaks: { per_second: number; peaks: number[] } | null;
  selected: string | null;
  onSeek: (t: number) => void; onSelect: (cueId: string) => void; onDropped: (c: Candidate) => void;
}

export function Timeline(p: Props) {
  const W = Math.max(200, p.duration * p.pps + 30);
  const scroll = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const cueOf = useRef<Record<string, string>>({});
  cueOf.current = Object.fromEntries(p.cues.flatMap((c) => c.sources.map((s) => [s, c.id])));

  // 看得见的那一段（横向位置、宽度）：波形只画这一段
  const [view, setView] = useState({ left: 0, width: 0 });
  useEffect(() => {
    const sc = scroll.current;
    if (!sc) return;
    const upd = () => setView({ left: sc.scrollLeft, width: sc.clientWidth });
    upd();
    sc.addEventListener("scroll", upd, { passive: true });
    const ro = new ResizeObserver(upd);
    ro.observe(sc);
    return () => { sc.removeEventListener("scroll", upd); ro.disconnect(); };
  }, []);

  // 波形：画布只和可见区域一样宽，滚动时重画。
  // 整条时间轴一样宽的画布，一小时的节目要六七万像素，超过浏览器的上限，什么也画不出来
  useEffect(() => {
    const cv = canvas.current;
    if (!cv || !view.width) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(view.width * dpr);
    cv.height = Math.round(36 * dpr);
    const g = cv.getContext("2d")!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, view.width, 36);
    g.fillStyle = "#6d6d80";
    const pk = p.peaks;
    if (!pk || !pk.peaks.length) return;
    for (let x = 0; x < view.width; x += 2) {
      const gx = view.left + x;
      const i0 = Math.floor((gx / p.pps) * pk.per_second), i1 = Math.max(i0 + 1, Math.floor(((gx + 2) / p.pps) * pk.per_second));
      let m = 0;
      for (let i = i0; i < i1 && i < pk.peaks.length; i++) m = Math.max(m, pk.peaks[i]);
      const h = Math.max(1, (m / 255) * 32);
      g.fillRect(x, 18 - h / 2, 1.5, h);
    }
  }, [p.peaks, p.pps, view]);

  // 拖动：在刻度 / 波形上拖 = 拖播放头；在其他地方拖 = 左右平移时间轴（不会选中文字）
  const drag = useRef<{ kind: "seek" | "pan"; x: number; left: number; moved: boolean } | null>(null);
  const timeAt = (clientX: number) => {
    const sc = scroll.current!;
    return Math.max(0, Math.min(p.duration, (clientX - sc.getBoundingClientRect().left + sc.scrollLeft) / p.pps));
  };
  const onDown = (e: React.MouseEvent, kind: "seek" | "pan") => {
    if (e.button !== 0) return;
    e.preventDefault();
    drag.current = { kind, x: e.clientX, left: scroll.current!.scrollLeft, moved: false };
    if (kind === "seek") p.onSeek(timeAt(e.clientX));
    const move = (ev: MouseEvent) => {
      const d = drag.current;
      if (!d) return;
      if (Math.abs(ev.clientX - d.x) > 3) d.moved = true;
      if (d.kind === "seek") p.onSeek(timeAt(ev.clientX));
      else scroll.current!.scrollLeft = d.left - (ev.clientX - d.x);
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      setTimeout(() => { drag.current = null; }, 0);  // 拖过就不算点击（不会顺手选中一条字幕）
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  const dragged = () => !!drag.current?.moved;

  // 播放时让播放头保持在可见范围
  useEffect(() => {
    const sc = scroll.current;
    if (!sc) return;
    const x = p.time * p.pps;
    if (x < sc.scrollLeft || x > sc.scrollLeft + sc.clientWidth - 40) sc.scrollLeft = Math.max(0, x - 80);
  }, [p.time, p.pps]);

  const blk = (x: { start: number; end: number }) => ({ left: x.start * p.pps, width: Math.max(6, (x.end - x.start) * p.pps - 2) });
  const sel = (srcId: string) => (cueOf.current[srcId] === p.selected ? "sel" : "");
  const clickCand = (c: Candidate) => {
    if (dragged()) return;
    const cid = cueOf.current[c.id];
    if (cid) p.onSelect(cid);
    else p.onDropped(c);
  };
  const lane = (cls: string, items: Candidate[]) => (
    <div className="lane" style={{ width: W }}>
      {items.map((c) => (
        <div key={c.id} className={`blk2 ${cls} ${c.dropped ? "drop" : ""} ${sel(c.id)}`} style={blk(c)}
             title={`${c.id} · ${c.text}${c.dropped ? ` · 丢弃：${c.dropped}` : ""}${c.dup_of ? " · 与字幕重复" : ""}`}
             onClick={() => clickCand(c)}>{c.text.replace(/\n/g, " ")}</div>
      ))}
    </div>
  );
  const ticks = [];
  const step = p.pps > 20 ? 5 : p.pps > 6 ? 15 : 60;
  for (let s = 0; s <= p.duration; s += step) ticks.push(s);

  return (
    <div className="timeline">
      <div className="tl-labels">
        <div className="rl" />
        <div className="wv"><i style={{ background: "#666" }} />波形</div>
        <div><i style={{ background: "var(--c-sub)" }} />字幕</div>
        <div><i style={{ background: "var(--c-ocr)" }} />画面字</div>
        <div><i style={{ background: "var(--c-asr)" }} />语音</div>
        <div><i style={{ background: "var(--c-fuse)" }} />原文</div>
        <div><i style={{ background: "var(--c-tr)" }} />译文</div>
      </div>
      <div className="tl-scroll" ref={scroll}>
        <div className="tl-inner" style={{ width: W }} onMouseDown={(e) => { if (!(e.target as HTMLElement).closest(".rl, .wv")) onDown(e, "pan"); }}>
          <div className="lane rl" style={{ width: W }} onMouseDown={(e) => onDown(e, "seek")}>
            {ticks.map((s) => <span key={s} style={{ left: s * p.pps }}>{fmt(s).slice(0, 5)}</span>)}
          </div>
          <div className="lane wv" style={{ width: W }} onMouseDown={(e) => onDown(e, "seek")}>
            <canvas ref={canvas} style={{ position: "absolute", top: 0, left: view.left, width: view.width, height: 36 }} />
          </div>
          {lane("sc", p.subs)}
          {lane("so", p.ocr)}
          {lane("sa", p.asr)}
          <div className="lane" style={{ width: W }}>
            {p.cues.map((c) => <div key={c.id} className={`blk2 sf ${c.id === p.selected ? "sel" : ""} ${c.review === "todo" ? "flag" : ""}`} style={blk(c)} onClick={() => { if (!dragged()) p.onSelect(c.id); }}>{c.orig.text}</div>)}
          </div>
          <div className="lane" style={{ width: W }}>
            {p.cues.map((c) => <div key={c.id} className={`blk2 st ${c.id === p.selected ? "sel" : ""}`} style={blk(c)} onClick={() => { if (!dragged()) p.onSelect(c.id); }}>{c.tr[p.target]?.text ?? ""}</div>)}
          </div>
          <div className="playhead" style={{ left: p.time * p.pps }} />
        </div>
      </div>
    </div>
  );
}
