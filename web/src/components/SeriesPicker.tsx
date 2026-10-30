import { useEffect, useState } from "react";
import { api } from "../api";
import type { SeriesInfo } from "../types";

/** 所属作品：从已有的作品里直接选，也可以新建一个（输入名字）。 */
export function SeriesPicker({ value, onChange, onPicked }: {
  value: string; onChange: (v: string) => void;
  onPicked?: (s: SeriesInfo | null) => void;   // 选中了哪个已有的作品（新建 / 不属于任何作品时为 null）
}) {
  const [list, setList] = useState<SeriesInfo[]>([]);
  const [typing, setTyping] = useState(false);
  useEffect(() => { api.series().then(setList).catch(() => {}); }, []);
  const known = list.find((x) => x.name === value);
  useEffect(() => { onPicked?.(known ?? null); }, [known?.name, known?.settings]); // eslint-disable-line react-hooks/exhaustive-deps
  const editing = typing || (!!value && !known && list.length > 0) || (!!value && list.length === 0);
  if (editing) {
    return (
      <div className="row" style={{ gap: 6 }}>
        <input className="inp" autoFocus={typing} placeholder="新作品的名字" value={value} onChange={(e) => onChange(e.target.value)} style={{ flex: 1 }} />
        {list.length > 0 && <button className="btn sm" onClick={() => { setTyping(false); onChange(known ? value : ""); }}>从已有的选</button>}
      </div>
    );
  }
  return (
    <select className="inp" value={value} onChange={(e) => {
      if (e.target.value === "\u0000new") { setTyping(true); onChange(""); } else onChange(e.target.value);
    }}>
      <option value="">（不属于任何作品）</option>
      {list.map((x) => (
        <option key={x.name} value={x.name}>
          {x.name}{x.episodes ? ` · ${x.episodes} 集` : ""}{x.terms ? ` · ${x.terms} 条术语` : ""}{x.settings ? " · 有默认设置" : ""}
        </option>
      ))}
      <option value={"\u0000new"}>＋ 新作品…</option>
    </select>
  );
}
