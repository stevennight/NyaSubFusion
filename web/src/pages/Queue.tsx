// 任务：一次加入多个文件 / 文件夹，按顺序处理。加入时选的和新建项目一样（作品、语言、预设）。
import { useEffect, useState } from "react";
import { api, subscribe } from "../api";
import { Modal } from "../components/Modal";
import { useSetup } from "../components/SetupWizard";
import { useStore } from "../store";
import type { QueueItem, QueueOptions, QueueState, TauriGlobal } from "../types";
import { pct, STAGE_LBL } from "../util";

const tauri = (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__;
import { MEDIA_EXTS } from "./Library";
const STATUS: Record<QueueItem["status"], [string, string]> = {
  pending: ["待处理", ""], running: ["运行中", "acc"], done: ["完成", "ok"], failed: ["失败", "bad"],
  waiting: ["等外部翻译", "warn"], cancelled: ["已取消", ""],
};

function dur(a: number | null, b: number | null) {
  if (!a) return "";
  const s = Math.max(0, (b ?? Date.now() / 1000) - a);
  return s < 60 ? `${Math.round(s)} 秒` : `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`;
}

export function QueuePage({ onOpenProject, addOpen = false, onAddClosed }: { onOpenProject: () => void; addOpen?: boolean; onAddClosed?: () => void }) {
  const s = useStore();
  const [q, setQ] = useState<QueueState | null>(null);
  const [adding, setAdding] = useState(addOpen);
  const [prog, setProg] = useState<Record<string, { done: number; total: number }>>({});
  const [, tick] = useState(0);

  useEffect(() => {
    api.queue().then(setQ).catch((e) => s.setError(e.message));
    const off = subscribe("_queue", (e) => {
      const ev = e as unknown as QueueState & { type: string; item?: string; name?: string; status?: string; done?: number; total?: number };
      if (ev.type === "queue") setQ({ paused: ev.paused, items: ev.items, running: ev.running });
      if (ev.type === "queue_progress" && ev.item) { const id = ev.item; setProg((m) => ({ ...m, [id]: { done: Number(ev.done ?? 0), total: Number(ev.total ?? 0) } })); }
      if (ev.type === "queue_stage" && ev.status === "running") setQ((cur) => cur && { ...cur, items: cur.items.map((i) => (i.id === ev.item ? { ...i, stage: ev.name ?? i.stage } : i)) });
    });
    const t = setInterval(() => tick((n) => n + 1), 1000);  // 运行中的用时
    return () => { off(); clearInterval(t); };
  }, []);

  const closeAdd = () => { setAdding(false); onAddClosed?.(); };
  const act = async (id: string, a: "remove" | "up" | "down" | "retry" | "cancel") => {
    try { setQ(await api.queueAction(id, a)); } catch (e) { s.setError((e as Error).message); }
  };
  if (!q) return <div className="empty">加载中…</div>;
  const counts = q.items.reduce<Record<string, number>>((m, i) => ({ ...m, [i.status]: (m[i.status] ?? 0) + 1 }), {});

  return (
    <div className="page"><div className="wide">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>任务</h2>
        <span className="dim">{q.items.length} 项 · 待处理 {counts.pending ?? 0} · 完成 {counts.done ?? 0}{counts.failed ? ` · 失败 ${counts.failed}` : ""}</span>
        <span className="sp" />
        {q.paused ? <span className="chip warn">已暂停</span> : q.running ? <span className="chip acc">● 处理中</span> : <span className="chip">空闲</span>}
        <button className="btn" onClick={async () => setQ(await api.pauseQueue(!q.paused))}>{q.paused ? "▶ 继续" : "❚❚ 暂停"}</button>
        <button className="btn" onClick={async () => setQ(await api.clearQueue())}>清除已完成</button>
        <button className="btn pri" onClick={() => setAdding(true)}>＋ 加入…</button>
      </div>

      <div className="card">
        {q.items.length === 0 ? <div className="dim">队列是空的。点右上角“加入…”一次加入多个视频或文件夹。</div> : (
          <table className="tb"><thead><tr><th>#</th><th>文件</th><th>状态</th><th>用时</th><th>说明</th><th /></tr></thead><tbody>
            {q.items.map((it, i) => {
              const [lbl, cls] = STATUS[it.status];
              return (
                <tr key={it.id}>
                  <td className="dim">{i + 1}</td>
                  <td><b>{it.name}</b><div className="dim" style={{ fontSize: 11.5, wordBreak: "break-all" }}>{it.video}</div></td>
                  <td style={{ whiteSpace: "nowrap" }}><span className={`chip ${cls}`}>{lbl}{it.status === "running" && it.stage ? ` · ${STAGE_LBL[it.stage] ?? it.stage}` : ""}{it.status === "running" && (it.stage === "mux" || it.stage === "burn") && prog[it.id] ? ` ${pct(prog[it.id])}%` : ""}</span></td>
                  <td className="dim" style={{ whiteSpace: "nowrap" }}>{dur(it.started, it.status === "running" ? null : it.finished)}</td>
                  <td style={{ fontSize: 12, color: it.error ? "var(--bad)" : "var(--tx2)", maxWidth: 320, wordBreak: "break-all" }}>{it.error ?? it.note ?? ""}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn sm ghost" title="上移" disabled={i === 0} onClick={() => act(it.id, "up")}>▲</button>
                    <button className="btn sm ghost" title="下移" disabled={i === q.items.length - 1} onClick={() => act(it.id, "down")}>▼</button>
                    {(it.status === "pending" || it.status === "running") && <button className="btn sm" onClick={() => act(it.id, "cancel")}>取消</button>}
                    {["failed", "cancelled", "done", "waiting"].includes(it.status) && <button className="btn sm" onClick={() => act(it.id, "retry")}>{it.status === "done" ? "再跑" : "重试"}</button>}
                    {it.status !== "running" && <button className="btn sm ghost" onClick={() => act(it.id, "remove")}>移除</button>}
                    <button className="btn sm" onClick={() => { s.open(it.pid); onOpenProject(); }}>打开</button>
                  </td>
                </tr>
              );
            })}
          </tbody></table>
        )}
        <div className="dim" style={{ fontSize: 12, marginTop: 8 }}>取消正在运行的项会在几秒内停下（已完成的步骤保留，重试时接着做）。程序重启后队列会恢复。</div>
      </div>
      {adding && <AddDialog onClose={closeAdd} onAdded={(r) => { setQ(r); closeAdd(); }} />}
    </div></div>
  );
}

/** 加入任务：选文件 / 文件夹，还没配置过的视频用这里选的（和新建项目一样），已经配置过的保持原样 */
function AddDialog({ onClose, onAdded }: { onClose: () => void; onAdded: (q: QueueState) => void }) {
  const s = useStore();
  const st = useSetup("", true);
  const [paths, setPaths] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [until, setUntil] = useState("render");
  const [video, setVideo] = useState<QueueOptions["video"]>("");
  const [busy, setBusy] = useState(false);
  const addPaths = (l: string[]) => setPaths((cur) => [...cur, ...l.filter((x) => !cur.includes(x))]);
  const pick = async (directory: boolean) => {
    const f = await tauri!.dialog.open(directory ? { directory: true } : { multiple: true, filters: [{ name: "视频 / 音频", extensions: MEDIA_EXTS }] });
    if (f) addPaths(Array.isArray(f) ? f : [f]);
  };
  // 桌面版：拖进对话框
  useEffect(() => {
    if (!tauri) return;
    let off: (() => void) | undefined;
    tauri.event.listen("tauri://drag-drop", (e) => addPaths(e.payload.paths ?? [])).then((u) => { off = u; });
    return () => off?.();
  }, []);
  const all = [...paths, ...text.split(/\r?\n/).map((x) => x.trim().replace(/^"|"$/g, "")).filter(Boolean)];
  const submit = async () => {
    setBusy(true);
    try {
      const r = await api.enqueue(all, { ...st.opts, until, video });
      s.toast(`已加入 ${r.added} 个${r.skipped ? `，${r.skipped} 个已经在队列里` : ""}`);
      onAdded(r);
    } catch (e) { s.setError((e as Error).message); } finally { setBusy(false); }
  };
  return <Modal onClose={onClose} width={900}>
    <h2 style={{ marginTop: 0 }}>加入任务</h2>
    <div className="grid2">
      <div>
        <h3>文件</h3>
        {paths.length > 0 && <div className="usedby" style={{ marginBottom: 8, maxHeight: 140, overflow: "auto" }}>
          {paths.map((x) => <div key={x}><span style={{ wordBreak: "break-all" }}>{x}</span><span className="sp" />
            <span className="link" onClick={() => setPaths(paths.filter((y) => y !== x))}>×</span></div>)}</div>}
        {tauri && <div className="row" style={{ marginBottom: 8 }}><button className="btn sm" onClick={() => pick(false)}>选择文件…</button>
          <button className="btn sm" onClick={() => pick(true)}>选择文件夹…</button><span className="hint">也可以拖进来</span></div>}
        <textarea className="inp" rows={3} placeholder="或者每行一个视频文件 / 文件夹路径（文件夹里的视频全部加入）" value={text} onChange={(e) => setText(e.target.value)} />
        <div className="hint" style={{ margin: "6px 0 14px" }}>下面的设置只用在还没配置过的视频上；已经配置过的保持原样。</div>
        {st.left}
      </div>
      <div>
        {st.right}
        <h3 style={{ marginTop: 14 }}>这一批</h3>
        <div className="kv">
          <span className="lbl">做到</span>
          <select className="inp" value={until} onChange={(e) => setUntil(e.target.value)}>
            {["fuse", "translate", "render"].map((x) => <option key={x} value={x}>{STAGE_LBL[x]}</option>)}</select>
          <span className="lbl">输出视频</span>
          <select className="inp" value={video} disabled={until !== "render"} onChange={(e) => setVideo(e.target.value as QueueOptions["video"])}>
            <option value="">按各集的设置</option><option value="off">这一批都不输出</option><option value="mux">这一批都封装 MKV（字幕 + 字体）</option><option value="burn">这一批都压制硬字幕</option></select>
        </div>
      </div>
    </div>
    <div className="row" style={{ marginTop: 14, justifyContent: "flex-end" }}>
      <button className="btn ghost" onClick={onClose}>取消</button>
      <button className="btn pri" disabled={busy || !all.length || !st.ok} onClick={submit}>{busy ? "加入中…" : "加入任务"}</button>
    </div>
  </Modal>;
}
