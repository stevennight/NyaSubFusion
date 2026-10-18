// GPU 加速组件（语音识别 / 画面字识别）：按需下载，所有项目共用，放在设置页。
import { useEffect, useState } from "react";
import { api, subscribe } from "../api";
import { useStore } from "../store";
import type { ComponentStatus } from "../types";

export function GpuComponents() {
  const s = useStore();
  const [st, setSt] = useState<ComponentStatus | null>(null);
  const [prog, setProg] = useState<{ pkg: string; done: number; total: number; phase?: string } | null>(null);
  const load = () => api.components().then(setSt).catch(() => setSt(null));
  useEffect(() => {
    load();
    return subscribe("_components", (e) => {
      const ev = e as unknown as { type: string; package?: string; done?: number; total?: number; status?: string; phase?: string };
      if (ev.type === "component") setProg((p) => ev.phase ? { pkg: p?.pkg ?? "", done: 0, total: 0, phase: ev.phase } : { pkg: ev.package!, done: ev.done!, total: ev.total! });
      if (ev.type === "job") { setProg(null); load(); s.toast(ev.status === "done" ? "组件已安装，重启程序后生效" : "组件安装失败"); }
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
      {prog && <div className="dim" style={{ marginTop: 8 }}>{prog.phase === "install" ? "下载完成，正在安装…"
        : `正在下载 ${prog.pkg}：${(prog.done / 2 ** 20).toFixed(0)} / ${prog.total ? (prog.total / 2 ** 20).toFixed(0) : "?"} MB`}</div>}
      {!prog && st.job?.status === "failed" && <div style={{ marginTop: 8, fontSize: 12, color: "var(--bad)", wordBreak: "break-all" }}>上次安装失败：{st.job.error}</div>}
    </div>
  );
}
