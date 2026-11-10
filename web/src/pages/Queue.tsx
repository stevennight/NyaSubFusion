// 任务：一次加入多个文件 / 文件夹，按顺序处理。加入时选的和新建项目一样（作品、语言、预设）。
import { useEffect, useState } from "react";
import { api, subscribe } from "../api";
import { Modal } from "../components/Modal";
import { useSetup } from "../components/SetupWizard";
import { useStore } from "../store";
import type { QueueItem, QueueOptions, QueueState, TauriGlobal } from "../types";
import { briefError, pct, STAGE_LBL } from "../util";

const tauri = (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__;
import { MEDIA_EXTS } from "./Library";
import { KIND_LBL } from "../components/RunBar";

/** 这一项是什么任务：一集里点的标出来；批量加入的写做到哪一步 */
function what(it: QueueItem) {
  const kind = it.kind ?? "run";
  const until = it.options.until && it.options.until !== "render" ? `（做到${STAGE_LBL[it.options.until] ?? it.options.until}）` : "";
  return (kind === "run" ? `运行${until}` : KIND_LBL[kind]) + (it.manual ? " · 单集" : " · 批量");
}
const STATUS: Record<QueueItem["status"], [string, string]> = {
  pending: ["待处理", ""], running: ["运行中", "acc"], done: ["完成", "ok"], failed: ["失败", "bad"],
  waiting: ["等外部翻译", "warn"], cancelled: ["已取消", ""],
};

type Filter = "all" | "active" | "failed" | "done" | "cancelled";
const FILTERS: [Filter, string][] = [["all", "全部"], ["active", "排队 / 运行中"], ["failed", "失败"], ["done", "完成"], ["cancelled", "已取消"]];
const PAGE = 20;
function inFilter(i: QueueItem, f: Filter) {
  if (f === "all") return true;
  if (f === "active") return i.status === "pending" || i.status === "running";
  if (f === "done") return i.status === "done" || i.status === "waiting";
  return i.status === f;
}
function when(t: number | null) {
  if (!t) return "";
  const d = new Date(t * 1000), now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? `今天 ${hm}` : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

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
  const [filter, setFilter] = useState<Filter>("all");
  const [pageSel, setPage] = useState(0);

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
  // 没做完的按执行顺序排在最上面（能调顺序），做完的按完成时间倒序（最近的在上）
  const isActive = (i: QueueItem) => i.status === "pending" || i.status === "running";
  const activeItems = q.items.filter(isActive);
  const activeIds = activeItems.map((i) => i.id);
  const finished = q.items.filter((i) => !isActive(i)).sort((a, b) => (b.finished ?? b.added) - (a.finished ?? a.added));
  const shown = [...activeItems, ...finished].filter((i) => inFilter(i, filter));
  const pages = Math.max(1, Math.ceil(shown.length / PAGE));
  const page = Math.min(pageSel, pages - 1);   // 移除、清除以后页数变少

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
        <div className="row" style={{ marginBottom: 8, gap: 4 }}>
          {FILTERS.map(([k, label]) => {
            const n = q.items.filter((i) => inFilter(i, k)).length;
            return <button key={k} className={`btn sm${filter === k ? " pri" : " ghost"}`} onClick={() => { setFilter(k); setPage(0); }}>{label} {n}</button>;
          })}
        </div>
        {shown.length === 0 ? <div className="dim">{q.items.length === 0 ? "队列是空的。点右上角“加入…”一次加入多个视频或文件夹。" : "没有这一类的任务。"}</div> : (
          <table className="tb"><thead><tr><th>文件</th><th>状态</th><th>时间</th><th>说明</th><th /></tr></thead><tbody>
            {shown.slice(page * PAGE, (page + 1) * PAGE).map((it) => {
              const [lbl, cls] = STATUS[it.status];
              const active = it.status === "pending" || it.status === "running";
              const k = activeIds.indexOf(it.id);
              return (
                <tr key={it.id}>
                  <td><b>{it.name}</b> <span className="chip">{what(it)}</span>
                    <div className="dim" style={{ fontSize: 11.5, wordBreak: "break-all" }}>{it.video}</div></td>
                  <td style={{ whiteSpace: "nowrap" }}><span className={`chip ${cls}`}>{lbl}{(it.status === "running" || it.status === "failed") && it.stage ? ` · ${STAGE_LBL[it.stage] ?? it.stage}` : ""}{it.status === "running" && (it.stage === "mux" || it.stage === "burn") && prog[it.id] ? ` ${pct(prog[it.id])}%` : ""}</span></td>
                  <td className="dim" style={{ whiteSpace: "nowrap", fontSize: 12 }}>{active ? (it.status === "running" ? dur(it.started, null) : `第 ${k + 1} 个`)
                    : <>{when(it.finished)}<br />{dur(it.started, it.finished)}</>}</td>
                  <td style={{ fontSize: 12, color: it.error ? "var(--bad)" : "var(--tx2)", maxWidth: 360, wordBreak: "break-all" }} title={it.error ?? ""}>{it.error ? briefError(it.error) : it.note ?? ""}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {it.status === "pending" && <>
                      <button className="btn sm ghost" title="上移" disabled={k <= 0} onClick={() => act(it.id, "up")}>▲</button>
                      <button className="btn sm ghost" title="下移" disabled={k === activeIds.length - 1} onClick={() => act(it.id, "down")}>▼</button></>}
                    {active && <button className="btn sm" onClick={() => act(it.id, "cancel")}>取消</button>}
                    {!active && <button className="btn sm" onClick={() => act(it.id, "retry")}>{it.status === "done" ? "再跑" : "重试"}</button>}
                    {it.status !== "running" && <button className="btn sm ghost" onClick={() => act(it.id, "remove")}>移除</button>}
                    <button className="btn sm" title={it.status === "failed" ? "打开这一集，在「处理」里点失败的那一步看完整日志" : ""} onClick={() => { s.open(it.pid); onOpenProject(); }}>打开</button>
                  </td>
                </tr>
              );
            })}
          </tbody></table>
        )}
        {pages > 1 && <div className="row" style={{ marginTop: 8, justifyContent: "center" }}>
          <button className="btn sm ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button>
          <span className="dim">{page + 1} / {pages}</span>
          <button className="btn sm ghost" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>下一页</button>
        </div>}
        <div className="dim" style={{ fontSize: 12, marginTop: 8 }}>所有任务都在这里排队，一次只做一件：批量加入的，和在一集里点的运行、封装、压制、导出广播字幕原样（这些插在批量加入的前面）。
          暂停只停批量加入的，一集里点的照常排队运行。取消正在运行的项会在几秒内停下（已完成的步骤保留，重试时接着做）。程序重启后队列会恢复。</div>
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
