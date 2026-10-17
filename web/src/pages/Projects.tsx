import { useEffect, useState } from "react";
import { api, subscribe } from "../api";
import { useStore } from "../store";
import { SetupWizard } from "../components/SetupWizard";
import type { ComponentStatus, ProjectEntry, ProjectSummary, TauriGlobal } from "../types";

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
        <h3>最近项目</h3>
        {recent.length === 0 ? <div className="dim">还没有项目</div> : (
          <table className="tb"><thead><tr><th>文件</th><th>位置</th><th>打开时间</th><th /></tr></thead><tbody>
            {recent.map((p) => (
              <tr key={p.id}>
                <td>{p.name}{!p.exists && <span className="chip bad" style={{ marginLeft: 6 }}>文件不存在</span>}</td>
                <td className="dim" style={{ wordBreak: "break-all" }}>{p.video}</td>
                <td className="dim">{new Date(p.opened * 1000).toLocaleString()}</td>
                <td><button className="btn sm" disabled={!p.exists} onClick={() => open(p.video)}>{s.pid === p.id ? "当前" : "打开"}</button></td>
              </tr>
            ))}
          </tbody></table>
        )}
      </div>
      <div className="gap" />
      <Components />
      {wizard && <SetupWizard summary={wizard} onCancel={() => setWizard(null)}
                              onDone={() => { const id = wizard.id; setWizard(null); s.open(id); onOpened(); }} />}
    </div></div>
  );
}

function Components() {
  const s = useStore();
  const [st, setSt] = useState<ComponentStatus | null>(null);
  const [prog, setProg] = useState<{ pkg: string; done: number; total: number } | null>(null);
  const load = () => api.components().then(setSt).catch(() => setSt(null));
  useEffect(() => {
    load();
    return subscribe("_components", (e) => {
      const ev = e as unknown as { type: string; package?: string; done?: number; total?: number; status?: string };
      if (ev.type === "component") setProg({ pkg: ev.package!, done: ev.done!, total: ev.total! });
      if (ev.type === "job") { setProg(null); load(); s.toast(ev.status === "done" ? "组件已安装，重启程序后生效" : "组件安装失败，见日志"); }
    });
  }, []);
  if (!st) return null;
  const busy = st.job?.status === "running" || !!prog;
  return (
    <div className="card">
      <h3>GPU 加速组件 <span className="dim" style={{ fontWeight: 400 }}>— 按需下载，不放进安装包</span></h3>
      <div className="dim" style={{ marginBottom: 10 }}>
        显卡：{st.gpu.name ?? "未检测到 NVIDIA 显卡（没有显卡也能用，只是慢）"} · 语音识别 CUDA 设备 {st.gpu.cuda_devices} 个 · 画面字识别 {st.gpu.ort_cuda ? "可用 CUDA" : "只用 CPU"}
      </div>
      {st.components.map((c) => (
        <div key={c.name} className="row" style={{ padding: "6px 0", borderTop: "1px solid #2a2a35" }}>
          <span className={`chip ${c.installed ? "ok" : ""}`}>{c.installed ? "已安装" : "未安装"}</span>
          <div style={{ flex: 1 }}><b>{c.label}</b><div className="dim" style={{ fontSize: 12 }}>{c.installed ? `${c.files} 个文件 · ${c.size_mb} MB` : c.detail}</div></div>
          {c.installed
            ? <button className="btn sm ghost" disabled={busy} onClick={() => api.installComponent(c.name, true).then(load)}>移除</button>
            : <button className="btn sm" disabled={busy || !st.gpu.name} onClick={() => api.installComponent(c.name).then(load)}>下载安装</button>}
        </div>
      ))}
      {prog && <div className="dim" style={{ marginTop: 8 }}>正在下载 {prog.pkg}：{(prog.done / 2 ** 20).toFixed(0)} / {prog.total ? (prog.total / 2 ** 20).toFixed(0) : "?"} MB</div>}
    </div>
  );
}
