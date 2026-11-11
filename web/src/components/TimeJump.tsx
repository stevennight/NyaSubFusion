// 播放条上的“当前时间 / 总长”：点一下（或按 G）变成输入框，输入时间回车跳过去，Esc 或点别处取消。
import { useEffect, useRef, useState } from "react";
import { fmt } from "../util";

/** 输入的时间 → 秒；认不出返回 null。
 * 35:33 / 35:33.7 / 1:02:03（冒号可以是全角「：」）；35m33s / 1h2m3s；
 * 只有数字：1～2 位是分钟（35 = 35:00），3～4 位是分秒（3533 = 35:33），5～6 位是时分秒（13533 = 1:35:33）；带小数点的是秒（90.5）。 */
export function parseTime(raw: string): number | null {
  const s = raw.trim().replace(/：/g, ":").replace(/[，、]/g, ".").replace(/\s+/g, "");
  if (!s) return null;
  if (s.includes(":")) {
    const parts = s.split(":");
    if (parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return null;
    return parts.reduce((acc, p) => acc * 60 + Number(p), 0);
  }
  const hms = s.match(/^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$/i);
  if (hms && (hms[1] || hms[2] || hms[3])) return Number(hms[1] ?? 0) * 3600 + Number(hms[2] ?? 0) * 60 + Number(hms[3] ?? 0);
  if (/^\d+\.\d+$/.test(s)) return Number(s);
  if (/^\d+$/.test(s)) {
    if (s.length <= 2) return Number(s) * 60;
    const sec = Number(s.slice(-2)), min = Number(s.slice(-4, -2)), h = Number(s.slice(0, -4) || 0);
    if (s.length > 6 || sec >= 60 || (s.length > 4 && min >= 60)) return null;
    return h * 3600 + min * 60 + sec;
  }
  return null;
}

export function TimeJump({ time, duration, onSeek, hotkey = false }: {
  time: number; duration: number; onSeek: (t: number) => void;
  hotkey?: boolean;   // 按 G 打开输入框（审校页；输入框、下拉框里打字时不算）
}) {
  const [text, setText] = useState<string | null>(null);   // null = 没在输入
  const [bad, setBad] = useState(false);
  const box = useRef<HTMLInputElement>(null);
  const open = () => { setText(fmt(time)); setBad(false); };
  useEffect(() => { if (text !== null) box.current?.select(); }, [text === null]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!hotkey) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.closest?.("input,textarea,select") || e.ctrlKey || e.altKey || e.metaKey) return;
      if (e.key === "g" || e.key === "G") { e.preventDefault(); open(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  const go = () => {
    const t = parseTime(text ?? "");
    if (t === null) { setBad(true); return; }
    onSeek(Math.max(0, duration ? Math.min(duration, t) : t));
    setText(null);
  };
  if (text === null) {
    return <span className="time jump" title={`点一下输入时间跳过去${hotkey ? "（快捷键 G）" : ""}`} onClick={open}>{fmt(time)} / {fmt(duration)}</span>;
  }
  return (
    <span className="time">
      <input ref={box} className={`inp jumpbox${bad ? " bad" : ""}`} value={text} autoFocus
             title="例如 35:33、1:02:03、35m33s、3533；回车跳过去，Esc 取消"
             onChange={(e) => { setText(e.target.value); setBad(false); }}
             onKeyDown={(e) => {
               e.stopPropagation();   // 空格、方向键这些不要触发播放器快捷键
               if (e.key === "Enter") go();
               if (e.key === "Escape") setText(null);
             }}
             onBlur={() => setText(null)} /> / {fmt(duration)}
    </span>
  );
}
