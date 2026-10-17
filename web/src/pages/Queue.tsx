// 批量队列：一次加入多个文件 / 文件夹，按顺序处理。
import { useEffect, useState } from "react";
import { api, subscribe } from "../api";
import { useStore } from "../store";
import type { QueueItem, QueueOptions, QueueState, TauriGlobal } from "../types";
import { pct, STAGE_LBL } from "../util";

const tauri = (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__;
const MEDIA_EXTS = ["mp4", "mkv", "ts", "m2ts", "mts", "mov", "webm", "avi", "flv", "wmv", "mp3", "wav", "flac", "m4a", "aac", "ogg"];
const STATUS: Record<QueueItem["status"], [string, string]> = {
  pending: ["待处理", ""], running: ["运行中", "acc"], done: ["完成", "ok"], failed: ["失败", "bad"],
  waiting: ["等外部翻译", "warn"], cancelled: ["已取消", ""],
};

function dur(a: number | null, b: number | null) {
  if (!a) return "";
  const s = Math.max(0, (b ?? Date.now() / 1000) - a);
  return s < 60 ? `${Math.round(s)} 秒` : `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`;
}

export function QueuePage({ onOpenProject }: { onOpenProject: () => void }) {
  const s = useStore();
  const [q, setQ] = useState<QueueState | null>(null);
  const [paths, setPaths] = useState("");
  const [presets, setPresets] = useState<{ id: string; name: string }[]>([]);
  const [opt, setOpt] = useState<QueueOptions>({ preset: "documentary", source: "auto", targets: ["zh-Hans"], series: "", until: "render", video: "" });
  const [prog, setProg] = useState<Record<string, { done: number; total: number }>>({});
  const [, tick] = useState(0);

  useEffect(() => {
    api.queue().then(setQ).catch((e) => s.setError(e.message));
    api.presets().then(setPresets);
    const off = subscribe("_queue", (e) => {
      const ev = e as unknown as QueueState & { type: string; item?: string; name?: string; status?: string; done?: number; total?: number };
      if (ev.type === "queue") setQ({ paused: ev.paused, items: ev.items, running: ev.running });
      if (ev.type === "queue_progress" && ev.item) { const id = ev.item; setProg((m) => ({ ...m, [id]: { done: Number(ev.done ?? 0), total: Number(ev.total ?? 0) } })); }
      if (ev.type === "queue_stage" && ev.status === "running") setQ((cur) => cur && { ...cur, items: cur.items.map((i) => (i.id === ev.item ? { ...i, stage: ev.name ?? i.stage } : i)) });
    });
    const t = setInterval(() => tick((n) => n + 1), 1000);  // 运行中的用时
    return () => { off(); clearInterval(t); };
  }, []);

  const add = async (list: string[]) => {
    if (!list.length) return;
    try {
      const r = await api.enqueue(list, opt);
      setQ(r);
      setPaths("");
      s.toast(`已加入 ${r.added} 个${r.skipped ? `，${r.skipped} 个已经在队列里` : ""}`);
    } catch (e) { s.setError((e as Error).message); }
  };
  const pick = async (directory: boolean) => {
    const f = await tauri!.dialog.open(directory ? { directory: true } : { multiple: true, filters: [{ name: "视频 / 音频", extensions: MEDIA_EXTS }] });
    if (f) add(Array.isArray(f) ? f : [f]);
  };
  const act = async (id: string, a: "remove" | "up" | "down" | "retry" | "cancel") => {
    try { setQ(await api.queueAction(id, a)); } catch (e) { s.setError((e as Error).message); }
  };
  if (!q) return <div className="empty">加载中…</div>;
  const counts = q.items.reduce<Record<string, number>>((m, i) => ({ ...m, [i.status]: (m[i.status] ?? 0) + 1 }), {});

  return (
    <div className="page"><div className="wide">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>批量队列</h2>
        <span className="dim">{q.items.length} 项 · 待处理 {counts.pending ?? 0} · 完成 {counts.done ?? 0}{counts.failed ? ` · 失败 ${counts.failed}` : ""}</span>
        <span className="sp" />
        {q.paused ? <span className="chip warn">已暂停</span> : q.running ? <span className="chip acc">● 处理中</span> : <span className="chip">空闲</span>}
        <button className="btn" onClick={async () => setQ(await api.pauseQueue(!q.paused))}>{q.paused ? "▶ 继续" : "❚❚ 暂停"}</button>
        <button className="btn" onClick={async () => setQ(await api.clearQueue())}>清除已完成</button>
      </div>

      <div className="card">
        <h3>加入队列</h3>
        <div className="grid2" style={{ gridTemplateColumns: "1.1fr .9fr" }}>
          <div>
            <textarea className="inp" rows={4} placeholder={"每行一个视频文件或文件夹路径（文件夹里的视频会全部加入）"} value={paths} onChange={(e) => setPaths(e.target.value)} />
            <div className="row" style={{ marginTop: 8 }}>
              {tauri && <><button className="btn" onClick={() => pick(false)}>选择文件…</button><button className="btn" onClick={() => pick(true)}>选择文件夹…</button></>}
              <span className="sp" />
              <button className="btn pri" disabled={!paths.trim()} onClick={() => add(paths.split(/\r?\n/).map((x) => x.trim().replace(/^"|"$/g, "")).filter(Boolean))}>加入队列</button>
            </div>
          </div>
          <div>
            <div className="lbl" style={{ marginBottom: 6 }}>还没配置过的视频用下面的设置（已经配置过的保持原样）</div>
            <div className="kv">
              <span className="lbl">预设</span>
              <select className="inp" value={opt.preset} onChange={(e) => setOpt({ ...opt, preset: e.target.value })}>{presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
              <span className="lbl">源语言</span>
              <select className="inp" value={opt.source} onChange={(e) => setOpt({ ...opt, source: e.target.value })}>
                <option value="auto">自动判断</option>{s.langs.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}</select>
              <span className="lbl">翻译成</span>
              <div className="row wrap">{s.langs.map((l) => {
                const on = opt.targets?.includes(l.code);
                return <span key={l.code} className={`chip ${on ? "acc" : ""}`} style={{ cursor: "pointer" }}
                             onClick={() => setOpt({ ...opt, targets: on ? opt.targets!.filter((t) => t !== l.code) : [...(opt.targets ?? []), l.code] })}>{l.name}</span>;
              })}</div>
              <span className="lbl">作品</span><input className="inp" value={opt.series} placeholder="可不填" onChange={(e) => setOpt({ ...opt, series: e.target.value })} />
              <span className="lbl">做到</span>
              <select className="inp" value={opt.until} onChange={(e) => setOpt({ ...opt, until: e.target.value })}>
                {["fuse", "translate", "render"].map((x) => <option key={x} value={x}>{STAGE_LBL[x]}</option>)}</select>
              <span className="lbl">输出视频</span>
              <select className="inp" value={opt.video} disabled={opt.until !== "render"} onChange={(e) => setOpt({ ...opt, video: e.target.value as QueueOptions["video"] })}>
                <option value="">按各项目的设置</option><option value="off">这一批都不输出</option><option value="mux">这一批都封装 MKV（字幕 + 字体）</option><option value="burn">这一批都压制硬字幕</option></select>
            </div>
          </div>
        </div>
      </div>
      <div className="gap" />
      <div className="card">
        {q.items.length === 0 ? <div className="dim">队列是空的</div> : (
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
        <div className="dim" style={{ fontSize: 12, marginTop: 8 }}>取消正在运行的项时，会等当前步骤做完再停下（语音 / 画面字识别不能中途打断）。程序重启后队列会恢复。</div>
      </div>
    </div></div>
  );
}
