// 多轨时间轴：波形 / 字幕 / 画面字 / 语音 三路候选 / 融合原文 / 译文。
//
// 哪些轨能改：只有最后一行「译文」（最终字幕 cues）可以在时间轴上直接拖。
// 「字幕 / 画面字 / 语音」是识别出来的参考，「原文」是同一批字幕的原文，这几行都只读（点一下 = 选中对应的字幕）。
//
// 「译文」轨上的手势（每个都对应审校面板里已有的操作，走同一个接口，可以 Ctrl+Z 撤销）：
//   拖左 / 右边缘（光标 ↔）  改这条的开始 / 结束；分了段的字幕改的是第一段的开始 / 最后一段的结束
//   拖段与段的分界线（↔）    改相邻两段的时间（前一段的结束 = 后一段的开始），整条的起止不变
//   拖中间（光标是手）       整条平移，各段跟着一起移
//   双击                     在双击处拆成两条字幕（同「在播放头拆成两条」）
//   Alt + 双击               在双击处分段：还是同一条字幕，多一屏（同「在播放头分段」）
//   分了段的字幕             每段单独显示自己的文字；点某一段 = 选中并跳到这一段；
//                            右键某一段：和前一段 / 后一段交换内容（改顺序）、从这里拆成单独的字幕
//   拖动时                   吸附到其他译文条的边、字幕 / 语音轨的边、播放头（6 像素以内），按住 Shift 不吸附；
//                            条上方显示当前时间，Esc 取消，松手才保存
//   限制                     每段至少 0.2 秒；开始、结束都不越过相邻条的开始、结束（不和相邻条颠倒）
// 其余照旧：点一下选中；在刻度 / 波形上拖 = 拖播放头；在空白处（包括参考轨）拖 = 左右平移。
import { useEffect, useRef, useState } from "react";
import type { Candidate, Cue, FusedItem } from "../types";
import { useStore } from "../store";
import { fmt, fmtHms } from "../util";

const ROW_H = 22;      // 展开的参考轨每行的高度
const OPEN_ROWS = 8;   // 展开的抽屉最多这么高，再多的在抽屉里上下滚动
type RefLane = "sc" | "so" | "sa";
const OPEN_KEY = "nyasub.tl.open";
const DROP_LBL: Record<string, string> = { junk: "杂讯", logo: "台标", scene: "实物上的字", ruby: "振假名" };

type Span = [number, number];
/** 「译文」条的新时间：和审校面板里改时间同一个接口（editCue 的 start / end / parts） */
export type CueTimes = { start: number; end: number; parts?: Span[] };
type Zone = { kind: "start" | "end" | "move" } | { kind: "seam"; k: number };
type Snap = { t: number; what: string };
/** 正在拖的那一条（松手前只是预览）；saving = 已松手、正在保存 */
interface Live { id: string; zone: Zone; start: number; end: number; parts: Span[] | null; snap: Snap | null; saving: boolean }

const MIN = 0.2;      // 每段最短（秒），和审校面板一致
const SNAP_PX = 6;    // 吸附范围（像素）
const r3 = (x: number) => Math.round(x * 1000) / 1000;
const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);
const near = (a: number, b: number) => Math.abs(a - b) < 0.0005;

interface Props {
  duration: number; time: number; pps: number;
  subs: Candidate[]; ocr: Candidate[]; asr: Candidate[]; fused: FusedItem[]; cues: Cue[]; target: string;
  peaks: { per_second: number; peaks: number[] } | null;
  selected: string | null;
  hint?: string | null;   // 将被合并进来的那一条（高亮）
  onSeek: (t: number) => void; onSelect: (cueId: string, at?: number) => void; onDropped: (c: Candidate) => void;
  // 不传就是只读（「译文」轨也不能拖）
  onCueTime?: (cueId: string, t: CueTimes) => Promise<void>;
  onCueSplit?: (cueId: string, at: number) => void;
  onCueCut?: (cueId: string, t: number) => void;
  onSegSwap?: (cueId: string, seg: number, other: number) => void;   // 两段互换内容（改顺序）
  onSegSplit?: (cueId: string, boundary: number) => void;            // 在第 boundary 个分界处拆成两条字幕
  // 参考轨（字幕 / 画面字 / 语音）上右键：以这一块新建一条关联的字幕 / 并入选中的字幕
  onSourceCue?: (c: Candidate) => void;
  onSourceAttach?: (c: Candidate, cueId: string, replace?: boolean) => void;
  refLanes?: boolean;                      // 显示参考轨（字幕 / 画面字 / 语音 / 原文）；false = 只留波形和译文
  onToggleRef?: () => void;
}

export function Timeline(p: Props) {
  const s = useStore();
  const showRef = p.refLanes ?? true;
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
  const drag = useRef<{ kind: "seek" | "pan" | "cue"; x: number; left: number; moved: boolean } | null>(null);
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

  // ---- 「译文」轨：拖边缘 / 分界线 / 整条 ----
  const root = useRef<HTMLDivElement>(null);
  const [live, setLive] = useState<Live | null>(null);
  const [menu, setMenu] = useState<
    { x: number; y: number; cue: string; seg: number; n: number; src?: undefined }
    | { x: number; y: number; src: Candidate; kind: string }
    | null>(null);
  // 参考轨上的文字复制出来，贴到已有的字幕里（不一定要新建一条）
  const copyText = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const t = document.createElement("textarea");
      t.value = text;
      document.body.appendChild(t);
      t.select();
      document.execCommand("copy");
      t.remove();
    }
    s.toast(`已复制「${text.replace(/\n/g, " ").slice(0, 16)}」`);
  };
  // 收起的参考轨上叠在一起的那几块：点「+N」列出来
  const [pile, setPile] = useState<{ x: number; y: number; kind: string; items: Candidate[] } | null>(null);
  useEffect(() => {
    if (!pile) return;
    const close = () => setPile(null);
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", close);
    return () => { window.removeEventListener("mousedown", close); window.removeEventListener("keydown", close); };
  }, [pile]);
  // 展开的那条参考轨（叠在一起的分行全铺开）：在时间轴上方浮出一个抽屉，不挤占时间轴的高度（1080p 上视频、字幕列表已经占满）。
  // 一次只开一条，记在本机
  const [open, setOpen] = useState<RefLane | null>(() => {
    try { const v = localStorage.getItem(OPEN_KEY); return v === "sc" || v === "so" || v === "sa" ? v : null; } catch { return null; }
  });
  const toggleOpen = (k: RefLane) => setOpen((o) => {
    const n = o === k ? null : k;
    try { if (n) localStorage.setItem(OPEN_KEY, n); else localStorage.removeItem(OPEN_KEY); } catch { /* 存不了就只在这次有效 */ }
    return n;
  });
  const [drawerAt, setDrawerAt] = useState<{ left: number; width: number; bottom: number } | null>(null);
  useEffect(() => {
    if (!open) return;
    const upd = () => {
      const sc = scroll.current?.getBoundingClientRect(), tl = root.current?.getBoundingClientRect();
      if (sc && tl) setDrawerAt({ left: sc.left, width: sc.width, bottom: window.innerHeight - tl.top + 4 });
    };
    upd();
    const ro = new ResizeObserver(upd);
    if (root.current) ro.observe(root.current);
    window.addEventListener("resize", upd);
    return () => { ro.disconnect(); window.removeEventListener("resize", upd); };
  }, [open]);
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", close);
    return () => { window.removeEventListener("mousedown", close); window.removeEventListener("keydown", close); };
  }, [menu]);
  const busy = useRef(false);   // 正在拖或正在保存：不接新的拖动
  const editable = !!p.onCueTime;
  const partsOf = (c: { parts?: Span[] | null }) => (c.parts && c.parts.length > 1 ? c.parts : null);

  // 鼠标在这一条的哪个位置：两端约 6 像素是边缘，分界线左右 4 像素，其余是整条
  const zoneAt = (e: React.MouseEvent<HTMLElement>, c: Cue): Zone => {
    const r = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - r.left;
    const edge = Math.min(6, r.width / 3);
    if (x <= edge) return { kind: "start" };
    if (x >= r.width - edge) return { kind: "end" };
    const ps = partsOf(c);
    if (ps) for (let k = 1; k < ps.length; k++) if (Math.abs(x - (ps[k][0] - c.start) * p.pps) <= 4) return { kind: "seam", k };
    return { kind: "move" };
  };

  const onCueDown = (e: React.MouseEvent<HTMLDivElement>, c: Cue) => {
    if (e.button !== 0) return;
    e.stopPropagation();   // 不让外层当成平移
    e.preventDefault();
    // preventDefault 不会让输入框失去焦点：先移开，拖完按 Ctrl+Z 才是撤销这次修改，而不是输入框里的文字
    const fe = document.activeElement as HTMLElement | null;
    if (fe?.closest?.("input, textarea, select")) fe.blur();
    if (busy.current) return;
    busy.current = true;
    const zone = zoneAt(e, c);
    // 相邻条只看同一类：说明类（贴在画面字旁边）和台词本来就会时间重叠，不能互相挡住
    const isInfo = (x: Cue) => ["info", "name", "title"].includes(x.type);
    const same = p.cues.filter((x) => isInfo(x) === isInfo(c));
    const i = same.findIndex((x) => x.id === c.id);
    const prev = same[i - 1], next = same[i + 1];
    const o = { start: c.start, end: c.end, parts: partsOf(c)?.map(([a, b]): Span => [a, b]) ?? null };
    const first = o.parts ? o.parts[0] : [o.start, o.end];
    const last = o.parts ? o.parts[o.parts.length - 1] : [o.start, o.end];
    // 吸附目标：其他译文条的边、字幕 / 语音轨的边、播放头
    const targets: Snap[] = [
      ...p.cues.filter((x) => x.id !== c.id).flatMap((x) => [{ t: x.start, what: "相邻译文" }, { t: x.end, what: "相邻译文" }]),
      ...p.subs.flatMap((x) => [{ t: x.start, what: "字幕轨" }, { t: x.end, what: "字幕轨" }]),
      ...p.asr.flatMap((x) => [{ t: x.start, what: "语音轨" }, { t: x.end, what: "语音轨" }]),
      { t: p.time, what: "播放头" },
    ];
    const snapTo = (t: number) => {
      let best: Snap | null = null, bd = SNAP_PX / p.pps;
      for (const s of targets) { const d = Math.abs(s.t - t); if (d <= bd) { bd = d; best = s; } }
      return best;
    };
    // 范围只在原来满足时才生效（原本就和相邻条交叠的说明类字幕也能拖）
    const lo = (v: number, bound: number) => Math.min(v, bound);
    const hi = (v: number, bound: number) => Math.max(v, bound);
    const x0 = e.clientX;

    const compute = (clientX: number, free: boolean): Live => {
      const dt = (clientX - x0) / p.pps;
      let start = o.start, end = o.end, snap: Snap | null = null;
      const parts = o.parts?.map(([a, b]): Span => [a, b]) ?? null;
      const edgeTo = (v0: number, a: number, b: number) => {
        let v = v0 + dt;
        const s = free ? snapTo(v) : null;
        if (s) v = s.t;
        v = clamp(v, a, b);
        snap = s && near(s.t, v) ? s : null;   // 被范围挡住了就不算吸附
        return r3(v);
      };
      if (zone.kind === "start") {
        start = edgeTo(o.start, lo(o.start, Math.max(0, prev?.start ?? 0)), hi(o.start, Math.min(first[1] - MIN, next?.start ?? Infinity)));
        if (parts) parts[0][0] = start;
      } else if (zone.kind === "end") {
        end = edgeTo(o.end, lo(o.end, Math.max(last[0] + MIN, prev?.end ?? 0)), hi(o.end, Math.min(p.duration || Infinity, next?.end ?? Infinity)));
        if (parts) parts[parts.length - 1][1] = end;
      } else if (zone.kind === "seam" && parts) {
        // 规则同审校面板改某段开始：这一段的开始 = 上一段的结束
        const k = zone.k, v0 = parts[k][0];
        const t = edgeTo(v0, lo(v0, parts[k - 1][0] + MIN), hi(v0, parts[k][1] - MIN));
        parts[k - 1][1] = t;
        parts[k][0] = t;
      } else {
        let d = dt;
        if (free) {
          const a = snapTo(o.start + dt), b = snapTo(o.end + dt);
          const da = a ? Math.abs(a.t - o.start - dt) : Infinity, db = b ? Math.abs(b.t - o.end - dt) : Infinity;
          if (a && da <= db) { d = a.t - o.start; snap = a; } else if (b) { d = b.t - o.end; snap = b; }
        }
        const dlo = Math.min(0, Math.max(-o.start, prev ? prev.start - o.start : -Infinity, prev ? prev.end - o.end : -Infinity));
        const dhi = Math.max(0, Math.min(p.duration ? p.duration - o.end : Infinity, next ? next.start - o.start : Infinity, next ? next.end - o.end : Infinity));
        const dc = clamp(d, dlo, dhi);
        if (!near(dc, d)) snap = null;
        start = r3(o.start + dc);
        end = r3(o.end + dc);
        if (parts) parts.forEach((sp) => { sp[0] = r3(sp[0] + dc); sp[1] = r3(sp[1] + dc); });
      }
      return { id: c.id, zone, start, end, parts, snap, saving: false };
    };

    const st = { kind: "cue" as const, x: x0, left: 0, moved: false };
    drag.current = st;
    root.current?.classList.add(zone.kind === "move" ? "dc-move" : "dc-ew");
    let cur: Live | null = null, lastX = x0;
    const move = (ev: MouseEvent) => {
      lastX = ev.clientX;
      if (!st.moved && Math.abs(ev.clientX - x0) <= 3) return;   // 没拖开就还是点击
      st.moved = true;
      cur = compute(ev.clientX, !ev.shiftKey);
      setLive(cur);
    };
    const key = (ev: KeyboardEvent) => {
      if (ev.key === "Escape" && ev.type === "keydown") { ev.preventDefault(); finish(false); }
      else if (ev.key === "Shift" && cur) { cur = compute(lastX, ev.type === "keyup"); setLive(cur); }
    };
    const finish = (ok: boolean) => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      window.removeEventListener("keydown", key);
      window.removeEventListener("keyup", key);
      root.current?.classList.remove("dc-ew", "dc-move");
      setTimeout(() => { if (drag.current === st) drag.current = null; }, 0);  // 拖过就不算点击
      const l = cur as Live | null;
      const segs = l ? l.parts ?? [[l.start, l.end] as Span] : [];
      const same = !l || (near(l.start, o.start) && near(l.end, o.end)
        && (!l.parts || l.parts.every(([a, b], k) => near(a, o.parts![k][0]) && near(b, o.parts![k][1]))));
      if (!ok || same || segs.some(([a, b]) => b - a < MIN - 1e-6)) { setLive(null); busy.current = false; return; }
      setLive({ ...l!, snap: null, saving: true });   // 保存完、重新加载之前先停在新位置
      p.onCueTime!(c.id, { start: l!.start, end: l!.end, ...(l!.parts ? { parts: l!.parts } : {}) })
        .finally(() => { setLive(null); busy.current = false; });
    };
    const up = () => finish(true);
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    window.addEventListener("keydown", key);
    window.addEventListener("keyup", key);
  };

  const onCueDouble = (e: React.MouseEvent<HTMLDivElement>, c: Cue) => {
    e.stopPropagation();
    if (busy.current) return;
    const t = r3(timeAt(e.clientX));
    if (e.altKey) p.onCueCut?.(c.id, t);
    else p.onCueSplit?.(c.id, t);
  };

  const tipOf = (l: Live) => {
    const z = l.zone, len = ` · ${(l.end - l.start).toFixed(2)}s`;
    const txt = z.kind === "start" ? `开始 ${fmtHms(l.start)}${len}` : z.kind === "end" ? `结束 ${fmtHms(l.end)}${len}`
      : z.kind === "seam" ? `分段 ${fmtHms(l.parts![z.k][0])}` : `${fmtHms(l.start)} → ${fmtHms(l.end)}`;
    const at = z.kind === "start" ? l.start : z.kind === "end" ? l.end : z.kind === "seam" ? l.parts![z.k][0] : (l.start + l.end) / 2;
    return { txt: txt + (l.snap ? ` · 吸附${l.snap.what}` : ""), x: at * p.pps };
  };

  // 播放时让播放头保持在可见范围
  useEffect(() => {
    const sc = scroll.current;
    if (!sc) return;
    const x = p.time * p.pps;
    if (x < sc.scrollLeft || x > sc.scrollLeft + sc.clientWidth - 40) sc.scrollLeft = Math.max(0, x - 80);
  }, [p.time, p.pps]);

  // 分了段的字幕：每段一块，放在这一段自己的时间上，显示自己的文字；点一下跳到这一段，右键有改顺序 / 拆开的菜单
  const segsOf = (c: Cue, ps: Span[] | null, text: string, x: { start: number }) => {
    if (!ps) return null;
    const pieces = text.split("｜").filter((t) => t.trim());
    if (pieces.length !== ps.length) return null;
    return ps.map(([a, b], k) => (
      <span key={k} className="segp" style={{ left: (a - x.start) * p.pps, width: Math.max(4, (b - a) * p.pps - 1) }}
            onClick={(e) => { e.stopPropagation(); if (!dragged()) p.onSelect(c.id, a + 0.05); }}
            onContextMenu={(e) => {
              e.preventDefault();
              e.stopPropagation();
              setMenu({ x: e.clientX, y: e.clientY, cue: c.id, seg: k, n: ps.length });
            }}>{pieces[k]}</span>
    ));
  };
  const blk = (x: { start: number; end: number }) => ({ left: x.start * p.pps, width: Math.max(6, (x.end - x.start) * p.pps - 2) });
  // 展开时：时间重叠的块分行放，不互相盖住（不限行数）
  const rowsOf = (items: { id: string; start: number; end: number }[]) => {
    const ends: number[] = [], row: Record<string, number> = {};
    for (const c of [...items].sort((a, b) => a.start - b.start)) {
      let r = ends.findIndex((e) => e <= c.start + 0.01);
      if (r < 0) r = ends.length;
      ends[r] = Math.max(ends[r] ?? 0, c.end);
      row[c.id] = r;
    }
    return { row, n: Math.max(1, ends.length) };
  };
  // 时间连着重叠的一组块（a 和 b 重叠、b 和 c 重叠 → 一组）
  const clustersOf = <T extends { start: number; end: number }>(items: T[]) => {
    const out: T[][] = [];
    let end = -1;
    for (const c of [...items].sort((a, b) => a.start - b.start)) {
      if (out.length && c.start < end - 0.01) out[out.length - 1].push(c);
      else out.push([c]);
      end = Math.max(out[out.length - 1].length === 1 ? c.end : end, c.end);
    }
    return out;
  };
  // 收起时：按“用进字幕的 > 没丢弃的 > 时间长的”挑出互不重叠的那些块整块显示（带文字），
  // 其余每块归到和它重叠最多的那块下面（底下画细线、那块左边标「+N」）。返回 [[显示的块, ...它下面压着的]]
  const piles = (items: Candidate[]) => {
    const rank = (c: Candidate) => [cueOf.current[c.id] ? 0 : 1, c.dropped ? 1 : 0, -(c.end - c.start)];
    const order = [...items].sort((a, b) => {
      const x = rank(a), y = rank(b);
      return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
    });
    const ov = (a: Candidate, b: Candidate) => Math.min(a.end, b.end) - Math.max(a.start, b.start);
    const groups: Candidate[][] = [], rest: Candidate[] = [];
    for (const c of order) {
      if (groups.some((g) => ov(g[0], c) > 0.01)) rest.push(c);
      else groups.push([c]);
    }
    for (const c of rest) groups.reduce((m, g) => (ov(g[0], c) > ov(m[0], c) ? g : m)).push(c);
    return groups;
  };
  const refRows = { sc: rowsOf(p.subs), so: rowsOf(p.ocr), sa: rowsOf(p.asr) };
  // 原文 / 译文轨不变高：同时显示的几条（很少）在这一行里上下平分
  const cueSlot: Record<string, { k: number; n: number }> = {};
  for (const g of clustersOf(p.cues)) {
    const r = rowsOf(g);
    for (const c of g) cueSlot[c.id] = { k: r.row[c.id], n: r.n };
  }
  const cueBox = (id: string): React.CSSProperties => {
    const sl = cueSlot[id];
    if (!sl || sl.n === 1) return {};
    const h = 19 / sl.n;
    return { top: 3 + sl.k * h, height: h - 1, lineHeight: `${h - 1}px`, fontSize: sl.n === 2 ? 9 : 0 };
  };
  const sel = (srcId: string) => (cueOf.current[srcId] === p.selected ? "sel" : "");
  const clickCand = (c: Candidate) => {
    if (dragged()) return;
    const cid = cueOf.current[c.id];
    if (cid) p.onSelect(cid);
    else p.onDropped(c);
  };
  const candDiv = (cls: RefLane, c: Candidate, kind: string, style: React.CSSProperties, extra = "", pileOf?: Candidate[]) => (
    <div key={c.id} className={`blk2 ${cls} ${c.dropped ? "drop" : ""} ${sel(c.id)} ${menu?.src?.id === c.id ? "menuon" : ""} ${extra}`}
         style={style}
         title={`${c.id} · ${c.text}${c.dropped ? ` · 丢弃：${c.dropped}` : ""}${c.ruby_of ? `（${c.ruby_of} 的注音）` : ""}${c.dup_of ? " · 与字幕重复" : ""}`
           + (pileOf ? `\n（这里还叠着 ${pileOf.length - 1} 块，点左边的 +${pileOf.length - 1} 看全部）` : "")}
         onClick={() => clickCand(c)}
         onContextMenu={(e) => {
           if (!p.onSourceCue && !p.onSourceAttach) return;
           e.preventDefault();
           setMenu({ x: e.clientX, y: e.clientY, src: c, kind });
         }}>
      {pileOf && <b className="pilen" title={`这里叠着 ${pileOf.length} 块：点开看全部`}
                    onMouseDown={(e) => e.stopPropagation()}
                    onClick={(e) => { e.stopPropagation(); setPile({ x: e.clientX, y: e.clientY, kind, items: pileOf }); }}>+{pileOf.length - 1}</b>}
      {c.text.replace(/\n/g, " ")}
    </div>
  );
  const LANES: [RefLane, string, string, Candidate[]][] = [["sc", "字幕", "var(--c-sub)", p.subs], ["so", "画面字", "var(--c-ocr)", p.ocr], ["sa", "语音", "var(--c-asr)", p.asr]];
  const drawer = () => {
    const ln = open && drawerAt ? LANES.find(([k]) => k === open) : null;
    if (!ln || !drawerAt) return null;
    const [k, name, , items] = ln, r = refRows[k];
    return (
      <div className="tl-drawer" style={{ left: drawerAt.left, width: drawerAt.width, bottom: drawerAt.bottom }}>
        <div className="tl-drawer-h">
          <span>{name}：叠在一起的分行全铺开（最多叠了 {r.n} 层）</span><span className="sp" />
          <button className="btn sm" onClick={() => toggleOpen(k)}>收起 ▾</button>
        </div>
        <div className="tl-drawer-b" style={{ height: Math.min(r.n, OPEN_ROWS) * ROW_H + 6 }}>
          <div style={{ position: "relative", width: W, height: r.n * ROW_H + 6, transform: `translateX(${-view.left}px)` }}>
            {items.map((c) => candDiv(k, c, name, { ...blk(c), top: 3 + r.row[c.id] * ROW_H }))}
            <div className="playhead" style={{ left: p.time * p.pps }} />
          </div>
        </div>
      </div>
    );
  };
  const lane = (cls: RefLane, items: Candidate[], kind: string) => {
    // 收起：叠在一起的只显示一块（带完整文字），其余的在底下画一道细线标出时间，左边「+N」点开看全部
    return (
      <div className="lane" style={{ width: W }}>
        {piles(items).flatMap(([top, ...under]) => {
          if (!under.length) return [candDiv(cls, top, kind, blk(top))];
          return [
            ...under.map((c) => <i key={c.id} className={`under ${cls} ${c.dropped ? "drop" : ""}`} style={blk(c)} />),
            candDiv(cls, top, kind, blk(top), "piled", [top, ...under]),
          ];
        })}
      </div>
    );
  };
  const ticks = [];
  const step = p.pps > 20 ? 5 : p.pps > 6 ? 15 : 60;
  for (let s = 0; s <= p.duration; s += step) ticks.push(s);

  return (
    <div className="timeline" ref={root}>
      {menu?.src && (() => {
        const c = menu.src, used = cueOf.current[c.id], selCue = p.cues.find((x) => x.id === p.selected);
        const inSel = !!selCue?.sources.includes(c.id);
        const close = (fn: () => void) => () => { fn(); setMenu(null); };
        return (
          <div className="tl-menu" style={{ left: Math.min(menu.x, window.innerWidth - 260), top: Math.min(menu.y, window.innerHeight - 240) }}
               onMouseDown={(e) => e.stopPropagation()}>
            <div className="dim">{menu.kind} {c.id}{used ? ` · 用在 #${used}` : " · 没有用到"}</div>
            <button disabled={!p.onSourceCue} title="时间用这一块的，原文用它的文字，配了大模型就顺手翻译"
                    onClick={close(() => p.onSourceCue?.(c))}>＋ 以此新建字幕</button>
            <button disabled={!p.onSourceAttach || !selCue || inSel}
                    title={!selCue ? "先在译文轨或字幕列表里选中一条字幕" : inSel ? "已经关联在这条字幕上了" : "原文按时间接上这一块的文字，时间放宽到包住它，整句重新翻译"}
                    onClick={close(() => selCue && p.onSourceAttach?.(c, selCue.id))}>
              ⇢ 并入选中的字幕{selCue ? ` #${selCue.id}` : ""}{inSel ? "（已关联）" : ""}</button>
            <button disabled={!p.onSourceAttach || !selCue}
                    title={!selCue ? "先在译文轨或字幕列表里选中一条字幕" : "原文换成这一块的文字（比如画面字省掉了半句、语音才完整），时间不变，整句重新翻译"}
                    onClick={close(() => selCue && p.onSourceAttach?.(c, selCue.id, true))}>
              ⇄ 用它替换选中字幕的原文{selCue ? ` #${selCue.id}` : ""}</button>
            <button title="复制这一块的文字，可以贴到已有的字幕里" onClick={close(() => copyText(c.text))}>⧉ 复制文字</button>
            {used && used !== p.selected && <button onClick={close(() => p.onSelect(used))}>选中用到它的字幕 #{used}</button>}
            <button onClick={close(() => p.onSeek(c.start))}>▶ 跳到这里</button>
          </div>
        );
      })()}
      {drawer()}
      {pile && (
        <div className="tl-menu tl-pile" style={{ left: Math.min(pile.x, window.innerWidth - 440),
                                                  ...(pile.y > window.innerHeight / 2 ? { bottom: window.innerHeight - pile.y + 6 } : { top: pile.y + 6 }) }}
             onMouseDown={(e) => e.stopPropagation()}>
          <div className="dim">{pile.kind}：这里叠着 {pile.items.length} 块（点一行选中，右键有更多操作）</div>
          {[...pile.items].sort((a, b) => a.start - b.start).map((c) => {
            const used = cueOf.current[c.id];
            return (
              <button key={c.id} className={c.dropped ? "dropped" : ""} title={c.text}
                      onClick={() => { setPile(null); clickCand(c); }}
                      onContextMenu={(e) => { e.preventDefault(); setPile(null); setMenu({ x: e.clientX, y: e.clientY, src: c, kind: pile.kind }); }}>
                <span className="pt">{c.text.replace(/\n/g, " / ")}</span>
                <span className="pm">{fmtHms(c.start)} · {c.id} · {used ? `用在 #${used}` : c.dropped ? `丢弃：${DROP_LBL[c.dropped] ?? c.dropped}` : "没有用到"}</span>
              </button>
            );
          })}
        </div>
      )}
      {menu && !menu.src && (
        <div className="tl-menu" style={{ left: menu.x, top: menu.y }} onMouseDown={(e) => e.stopPropagation()}>
          <div className="dim">第 {menu.seg + 1} 段（共 {menu.n} 段）</div>
          <button disabled={menu.seg === 0 || !p.onSegSwap} onClick={() => { p.onSegSwap?.(menu.cue, menu.seg, menu.seg - 1); setMenu(null); }}>← 和前一段交换</button>
          <button disabled={menu.seg === menu.n - 1 || !p.onSegSwap} onClick={() => { p.onSegSwap?.(menu.cue, menu.seg, menu.seg + 1); setMenu(null); }}>和后一段交换 →</button>
          <button disabled={menu.seg === 0 || !p.onSegSplit} onClick={() => { p.onSegSplit?.(menu.cue, menu.seg); setMenu(null); }}>✂ 从这一段开头拆成单独的字幕</button>
        </div>
      )}
      <div className="tl-labels">
        <div className="rl">
          {p.onToggleRef && <button className="tl-ref" title={showRef ? "收起参考轨（字幕 / 画面字 / 语音 / 原文），只留波形和译文" : "展开参考轨"}
                                    onClick={p.onToggleRef}>{showRef ? "▾ 参考轨" : "▸ 参考轨"}</button>}
        </div>
        <div className="wv"><i style={{ background: "#666" }} />波形</div>
        {showRef && <>
          {LANES.map(([k, name, color]) => (
            <div key={k} className={`lbl-lane ${open === k ? "on" : ""}`}>
              <i style={{ background: color }} />{name}
              {refRows[k].n > 1 && <button className="tl-open" onClick={() => toggleOpen(k)}
                                           title={open === k ? "收起" : `在上方展开：叠在一起的分行全铺开（最多叠了 ${refRows[k].n} 层），不占时间轴的高度`}>
                {open === k ? "▴" : "▸"}</button>}
            </div>
          ))}
          <div><i style={{ background: "var(--c-fuse)" }} />原文</div>
        </>}
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
          {showRef && <>
            {lane("sc", p.subs, "字幕")}
            {lane("so", p.ocr, "画面字")}
            {lane("sa", p.asr, "语音")}
            <div className="lane" style={{ width: W }}>
              {p.cues.map((c) => <div key={c.id} className={`blk2 sf ${c.id === p.selected ? "sel" : ""} ${c.id === p.hint ? "hint" : ""} ${c.review === "todo" ? "flag" : c.review === "flag" ? "marked" : ""}`} style={{ ...blk(c), ...cueBox(c.id) }} onClick={() => { if (!dragged()) p.onSelect(c.id); }}>
                {segsOf(c, partsOf(c), c.orig.text, c) ?? c.orig.text}
              </div>)}
            </div>
          </>}
          <div className="lane" style={{ width: W }}>
            {p.cues.map((c) => {
              const lv = live?.id === c.id ? live : null;
              const x = lv ?? c;
              const ps = lv ? lv.parts : partsOf(c);
              return (
                <div key={c.id} className={`blk2 st ${editable ? "edit" : ""} ${lv ? "live" : ""} ${c.id === p.selected ? "sel" : ""} ${c.id === p.hint ? "hint" : ""}`}
                     style={{ ...blk(x), ...cueBox(c.id) }}
                     title={editable ? "拖两端改开始 / 结束，拖中间整条平移，拖分界线改分段；双击拆成两条，Alt+双击分段（按住 Shift 不吸附）" : undefined}
                     onMouseDown={editable ? (e) => onCueDown(e, c) : undefined}
                     onMouseMove={editable ? (e) => { if (!busy.current) e.currentTarget.style.cursor = zoneAt(e, c).kind === "move" ? "grab" : "ew-resize"; } : undefined}
                     onDoubleClick={editable ? (e) => onCueDouble(e, c) : undefined}
                     onClick={() => { if (!dragged()) p.onSelect(c.id); }}>
                  {ps?.slice(1).map(([a], k) => <i key={k} className="seam" style={{ left: (a - x.start) * p.pps }} />)}
                  {segsOf(c, ps, c.tr[p.target]?.text ?? "", x) ?? c.tr[p.target]?.text ?? ""}
                </div>
              );
            })}
            {live && !live.saving && (() => {
              // 靠近可见区域两头时提示往里贴，不被裁掉
              const tp = tipOf(live), rel = tp.x - view.left;
              const tf = rel > view.width - 160 ? "translateX(-100%)" : rel < 160 ? "none" : undefined;
              return <div className="tl-dragtip" style={{ left: tp.x, ...(tf ? { transform: tf } : {}) }}>{tp.txt}</div>;
            })()}
          </div>
          <div className="playhead" style={{ left: p.time * p.pps }} />
          {live?.snap && <div className="snapline" style={{ left: live.snap.t * p.pps }} />}
        </div>
      </div>
    </div>
  );
}
