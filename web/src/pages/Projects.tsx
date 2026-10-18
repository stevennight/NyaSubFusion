import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { SetupWizard } from "../components/SetupWizard";
import type { ProjectEntry, ProjectSummary, TauriGlobal } from "../types";
import { langName, STAGE_LBL } from "../util";

const tauri = (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__;
const MEDIA_EXTS = ["mp4", "mkv", "ts", "m2ts", "mts", "mov", "webm", "avi", "flv", "wmv", "mp3", "wav", "flac", "m4a", "aac", "ogg"];

export function ProjectsPage({ onOpened }: { onOpened: () => void }) {
  const s = useStore();
  const [path, setPath] = useState("");
  const [recent, setRecent] = useState<ProjectEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [wizard, setWizard] = useState<ProjectSummary | null>(null);

  useEffect(() => { api.projects().then(setRecent).catch((e) => s.setError(e.message)); }, [s]);

  const open = async (p: string) => {
    setBusy(true);
    try {
      const sum = await api.open(p.trim().replace(/^"|"$/g, ""));
      if (sum.configured === false) {
        setWizard(sum);  // 第一次打开：先选语言、作品、预设
      } else {
        s.open(sum.id);
        onOpened();
      }
    } catch (e) {
      s.setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // 桌面版：拖入文件
  useEffect(() => {
    if (!tauri) return;
    let off: (() => void) | undefined;
    tauri.event.listen("tauri://drag-drop", (e) => { const f = e.payload.paths?.[0]; if (f) open(f); }).then((u) => { off = u; });
    return () => off?.();
  }, []);

  const pick = async () => {
    const f = await tauri!.dialog.open({ multiple: false, filters: [{ name: "视频 / 音频", extensions: MEDIA_EXTS }] });
    if (typeof f === "string") open(f);
  };

  return (
    <div className="page"><div className="wide">
      <h2>打开项目</h2>
      <div className="drop-zone">
        <strong style={{ color: "var(--tx)", fontSize: 15 }}>{tauri ? "把视频 / 音频拖到窗口里，或选择文件" : "输入视频 / 音频文件路径"}</strong>
        <div className="dim" style={{ margin: "4px 0 12px" }}>{MEDIA_EXTS.slice(0, 11).join(" · ")}</div>
        <div className="row" style={{ maxWidth: 760, margin: "0 auto" }}>
          {tauri && <button className="btn pri" disabled={busy} onClick={pick}>选择文件…</button>}
          <input className="inp" placeholder="或输入路径，例如 D:\Videos\海辺の暮らし_第3集.ts" value={path} onChange={(e) => setPath(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && path) open(path); }} />
          <button className={`btn ${tauri ? "" : "pri"}`} disabled={!path || busy} onClick={() => open(path)}>{busy ? "打开中…" : "打开"}</button>
        </div>
      </div>
      <div className="gap" />
      <div className="card">
        <div className="row" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>最近项目</h3><span className="dim">{recent.length} 个</span>
        </div>
        {recent.length === 0 ? <div className="dim">还没有项目。打开一个视频就会出现在这里。</div> : (
          <div className="plist">
            {recent.map((p) => <ProjectRow key={p.id} p={p} current={s.pid === p.id} onOpen={() => open(p.video)}
                                             onForget={async () => setRecent(await api.forget(p.id))} />)}
          </div>
        )}
      </div>
      {wizard && <SetupWizard summary={wizard} onCancel={() => setWizard(null)}
                              onDone={() => { const id = wizard.id; setWizard(null); s.open(id); onOpened(); }} />}
    </div></div>
  );
}

function ProjectRow({ p, current, onOpen, onForget }: { p: ProjectEntry; current: boolean; onOpen: () => void; onForget: () => void }) {
  const s = useStore();
  const mins = p.duration ? `${Math.floor(p.duration / 60)} 分 ${String(Math.round(p.duration % 60)).padStart(2, "0")} 秒` : "";
  const langs = p.configured
    ? `${p.source && p.source !== "auto" ? langName(p.source, s.langs) : "自动判断"} → ${(p.targets ?? []).map((t) => langName(t, s.langs)).join("、") || "—"}`
    : "还没配置";
  const state = p.running ? <span className="chip acc">● 运行中</span>
    : p.outputs?.length ? <span className="chip ok" title={p.outputs.join("\n")}>已输出字幕 · {p.outputs.length} 个文件</span>
    : p.reached ? <span className="chip">做到：{STAGE_LBL[p.reached] ?? p.reached}</span>
    : <span className="chip">未运行</span>;
  return (
    <div className={`prow ${current ? "cur" : ""} ${p.exists ? "" : "gone"}`}>
      <div className="pmain" onClick={p.exists ? onOpen : undefined} title={p.video}>
        <div className="row" style={{ gap: 8 }}>
          <b className="pname">{p.name}</b>
          {current && <span className="chip acc">当前</span>}
          {!p.exists && <span className="chip bad">文件不存在</span>}
        </div>
        <div className="pmeta">
          <span>📁 {p.folder}</span>
          {mins && <span>{mins}</span>}
          <span>{langs}</span>
          {p.series && <span>作品：{p.series}</span>}
          <span>打开于 {new Date(p.opened * 1000).toLocaleString()}</span>
        </div>
      </div>
      <div className="pside">
        {state}
        <button className="btn sm" disabled={!p.exists} onClick={onOpen}>{current ? "回到项目" : "打开"}</button>
        <button className="btn sm ghost" title="在资源管理器里打开所在文件夹" onClick={() => api.reveal(p.id).catch((e) => s.setError(e.message))}>文件夹</button>
        <button className="btn sm ghost" title="只从列表里去掉，不删除任何文件" onClick={onForget}>移除</button>
      </div>
    </div>
  );
}
