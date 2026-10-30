// 库：按作品分组列出各集。点作品进作品页（各集、默认设置、术语）；点一集打开它。拖进视频新建项目。
import { useEffect, useState } from "react";
import { api } from "../api";
import { SetupWizard } from "../components/SetupWizard";
import { useStore } from "../store";
import type { ProjectEntry, ProjectSummary, SeriesInfo, TauriGlobal } from "../types";
import { langName, STAGE_LBL } from "../util";

const tauri = (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__;
export const MEDIA_EXTS = ["mp4", "mkv", "ts", "m2ts", "mts", "mov", "webm", "avi", "flv", "wmv", "mp3", "wav", "flac", "m4a", "aac", "ogg"];

/** 打开一个视频：还没配置过的先走新建向导。series：从作品页“加一集”进来时预先选好作品。 */
export function useOpenVideo(onOpened: () => void) {
  const s = useStore();
  const [busy, setBusy] = useState(false);
  const [wizard, setWizard] = useState<{ sum: ProjectSummary; series: string } | null>(null);
  const open = async (path: string, series = "") => {
    setBusy(true);
    try {
      const sum = await api.open(path.trim().replace(/^"|"$/g, ""));
      if (sum.configured === false) setWizard({ sum, series });
      else { s.open(sum.id); onOpened(); }
    } catch (e) { s.setError((e as Error).message); } finally { setBusy(false); }
  };
  const pick = async (series = "") => {
    if (tauri) {
      const f = await tauri.dialog.open({ multiple: false, filters: [{ name: "视频 / 音频", extensions: MEDIA_EXTS }] });
      if (typeof f === "string") open(f, series);
    } else {
      const f = prompt("视频 / 音频文件的路径");
      if (f) open(f, series);
    }
  };
  const ui = wizard && <SetupWizard summary={wizard.sum} series={wizard.series} onCancel={() => setWizard(null)}
                                    onDone={() => { const id = wizard.sum.id; setWizard(null); s.open(id); onOpened(); }} />;
  return { open, pick, busy, ui };
}

export function LibraryPage({ onOpened, onSeries, onQueue }: { onOpened: () => void; onSeries: (name: string) => void; onQueue: () => void }) {
  const s = useStore();
  const [list, setList] = useState<ProjectEntry[]>([]);
  const [series, setSeries] = useState<SeriesInfo[]>([]);
  const [q, setQ] = useState("");
  const [path, setPath] = useState("");
  const ov = useOpenVideo(onOpened);
  const load = () => Promise.all([api.projects(), api.series()]).then(([p, sr]) => { setList(p); setSeries(sr); }).catch((e) => s.setError(e.message));
  useEffect(() => { load(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  // 桌面版：拖入文件
  useEffect(() => {
    if (!tauri) return;
    let off: (() => void) | undefined;
    tauri.event.listen("tauri://drag-drop", (e) => { const f = e.payload.paths?.[0]; if (f) ov.open(f); }).then((u) => { off = u; });
    return () => off?.();
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const match = (p: ProjectEntry) => !q || [p.name, p.series, p.folder].some((x) => x?.toLowerCase().includes(q.toLowerCase()));
  const shown = list.filter(match);
  const groups = [...new Set([...series.map((x) => x.name), ...list.map((p) => p.series ?? "").filter(Boolean)])]
    .filter((n) => !q || n.toLowerCase().includes(q.toLowerCase()) || shown.some((p) => p.series === n));
  const loose = shown.filter((p) => !p.series);
  return (
    <div className="page"><div className="wide">
      <div className="row" style={{ marginBottom: 14 }}>
        <h2 style={{ margin: 0 }}>库</h2><span className="dim">{groups.length} 部作品 · {list.length} 集</span><span className="sp" />
        <input className="inp" style={{ width: 220 }} placeholder="搜索作品、集…" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn" onClick={onQueue} title="一次加入多个文件 / 文件夹，在「任务」里排队处理">批量加入…</button>
        <button className="btn pri" disabled={ov.busy} onClick={() => ov.pick()}>＋ 新建项目</button>
      </div>
      <div className="drop-zone" style={{ padding: 14, marginBottom: 16 }}>
        <span>{tauri ? "把视频 / 音频拖到窗口里新建项目，或者输入路径：" : "输入视频 / 音频文件的路径："}</span>
        <div className="row" style={{ maxWidth: 760, margin: "8px auto 0" }}>
          <input className="inp" placeholder="例如 D:\Videos\海辺の暮らし_第3集.ts" value={path} onChange={(e) => setPath(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && path) ov.open(path); }} />
          <button className="btn" disabled={!path || ov.busy} onClick={() => ov.open(path)}>{ov.busy ? "打开中…" : "打开"}</button>
        </div>
      </div>
      {groups.map((name) => {
        const info = series.find((x) => x.name === name);
        const eps = shown.filter((p) => p.series === name);
        return (
          <div key={name} className="series-card">
            <div className="sh" onClick={() => onSeries(name)}>
              <b>{name}</b><span className="chip">{list.filter((p) => p.series === name).length} 集</span>
              {info?.settings ? <span className="chip tpl">有默认设置</span> : <span className="chip">还没有默认设置</span>}
              {info?.terms ? <span className="chip">术语 {info.terms}</span> : null}
              <span className="sp" /><span className="dim">进入作品 ›</span>
            </div>
            {eps.map((p) => <EpRow key={p.id} p={p} current={s.pid === p.id} onOpen={() => ov.open(p.video)} onChanged={load} />)}
            <div className="ep add" onClick={() => ov.pick(name)}>＋ 给这部作品加一集{info?.settings ? "（复制作品的默认设置）" : ""}</div>
          </div>
        );
      })}
      {(loose.length > 0 || groups.length === 0) && (
        <div className="series-card">
          <div className="sh plain"><b>未归入作品</b><span className="chip">{loose.length} 个</span><span className="sp" /><span className="dim">每个项目独立设置、独立术语表</span></div>
          {loose.map((p) => <EpRow key={p.id} p={p} current={s.pid === p.id} onOpen={() => ov.open(p.video)} onChanged={load} />)}
          {loose.length === 0 && <div className="ep dim">还没有项目。打开一个视频就会出现在这里。</div>}
        </div>
      )}
      {ov.ui}
    </div></div>
  );
}

function EpRow({ p, current, onOpen, onChanged }: { p: ProjectEntry; current: boolean; onOpen: () => void; onChanged: () => void }) {
  const s = useStore();
  const langs = p.configured
    ? `${p.source && p.source !== "auto" ? langName(p.source, s.langs) : "自动判断"} → ${(p.targets ?? []).map((t) => langName(t, s.langs)).join("、") || "—"}`
    : "还没配置";
  const [lbl, cls, dot] = p.running ? ["运行中", "acc", "run"] : p.outputs?.length ? [`已输出 ${p.outputs.length} 个文件`, "ok", "done"]
    : p.reached ? [`做到：${STAGE_LBL[p.reached] ?? p.reached}`, "", "stale"] : ["未运行", "", "na"];
  const mins = p.duration ? `${Math.floor(p.duration / 60)} 分 ${String(Math.round(p.duration % 60)).padStart(2, "0")} 秒` : "";
  return (
    <div className={`ep${p.exists ? "" : " gone"}`} onClick={p.exists ? onOpen : undefined} title={p.video}>
      <span className={`dot ${dot}`} />
      <div className="epname"><b>{p.name}</b>{current && <span className="chip acc mini">当前</span>}{!p.exists && <span className="chip bad mini">文件不存在</span>}
        <small>📁 {p.folder}{mins ? ` · ${mins}` : ""} · 打开于 {new Date(p.opened * 1000).toLocaleDateString()}</small></div>
      <span className="dim">{langs}</span>
      <span className={`chip ${cls}`} title={p.outputs?.join("\n")}>{lbl}</span>
      <span className="row" style={{ gap: 4 }} onClick={(e) => e.stopPropagation()}>
        <button className="btn sm ghost" title="在资源管理器里打开所在文件夹" onClick={() => api.reveal(p.id).catch((e) => s.setError(e.message))}>文件夹</button>
        <button className="btn sm ghost" title="只从列表里去掉，不删除任何文件" onClick={async () => { await api.forget(p.id); onChanged(); }}>移除</button>
      </span>
    </div>
  );
}
