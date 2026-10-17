// 新建项目向导：第一次打开一个视频（还没有 project.toml）时显示。
import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { ProjectSummary } from "../types";

export function SetupWizard({ summary, onDone, onCancel }: { summary: ProjectSummary; onDone: () => void; onCancel: () => void }) {
  const s = useStore();
  const [presets, setPresets] = useState<{ id: string; name: string; desc: string }[]>([]);
  const [series, setSeries] = useState<{ name: string; terms: number; episodes: number }[]>([]);
  const [preset, setPreset] = useState("documentary");
  const [source, setSource] = useState("auto");
  const [targets, setTargets] = useState<string[]>(["zh-Hans"]);
  const [seriesName, setSeriesName] = useState("");
  const [context, setContext] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.presets().then(setPresets); api.series().then(setSeries); }, []);

  const p = summary.probe ?? {};
  const textSubs = (p.subtitles ?? []).filter((t) => !["hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"].includes(t.codec));
  const create = async () => {
    setBusy(true);
    try {
      await api.setup(summary.id, { preset, source, targets, series: seriesName.trim(), context });
      onDone();
    } catch (e) { s.setError((e as Error).message); } finally { setBusy(false); }
  };
  const toggle = (code: string) => setTargets(targets.includes(code) ? targets.filter((t) => t !== code) : [...targets, code]);

  return (
    <div style={{ position: "fixed", inset: 0, background: "#000a", display: "grid", placeItems: "center", zIndex: 60 }}>
      <div className="card" style={{ width: 860, maxHeight: "90vh", overflow: "auto" }}>
        <div className="row" style={{ marginBottom: 10 }}>
          <h2 style={{ margin: 0 }}>新建项目</h2><span className="dim">{summary.name}</span>
          {p.duration ? <span className="chip">{Math.floor(p.duration / 60)} 分 {Math.round(p.duration % 60)} 秒</span> : null}
        </div>
        <div className="grid2">
          <div>
            <h3>检测到的来源</h3>
            {p.error ? <div className="chip bad">{p.error}</div> : (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div className="row"><span className="badge b-c">字幕</span>
                  {p.external_subs ? `同名外挂字幕：${p.external_subs.split(/[\\/]/).pop()}`
                    : textSubs.length ? `内封字幕 ${textSubs.length} 条：${textSubs.map((t) => `${t.codec}${t.lang ? " " + t.lang : ""}`).join("、")}`
                    : p.arib_ttml ? "可能有 4K 广播字幕（ARIB-TTML）" : <span className="dim">没有找到</span>}</div>
                <div className="row"><span className="badge b-a">语音</span>{p.has_audio ? "有音轨" : <span className="dim">没有音轨</span>}</div>
                <div className="row"><span className="badge b-o">画面字</span>{p.video ? `${p.video.width}×${p.video.height} ${p.video.codec ?? ""}` : <span className="dim">纯音频，不识别画面字</span>}</div>
              </div>
            )}
            <h3 style={{ marginTop: 14 }}>语言</h3>
            <div className="kv">
              <span className="lbl">源语言</span>
              <select className="inp" value={source} onChange={(e) => setSource(e.target.value)}>
                <option value="auto">自动判断（按语音 / 字幕）</option>
                {s.langs.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
              </select>
              <span className="lbl">翻译成</span>
              <div className="row wrap">{s.langs.map((l) => (
                <label key={l.code} className={`chip ${targets.includes(l.code) ? "acc" : ""}`} style={{ cursor: "pointer" }}>
                  <input type="checkbox" checked={targets.includes(l.code)} onChange={() => toggle(l.code)} style={{ display: "none" }} />{l.name}
                </label>))}</div>
            </div>
            <h3 style={{ marginTop: 14 }}>作品</h3>
            <input className="inp" list="wizard-series" placeholder="同一作品的各集共享术语表、读音词典（可不填）" value={seriesName} onChange={(e) => setSeriesName(e.target.value)} />
            <datalist id="wizard-series">{series.map((x) => <option key={x.name} value={x.name}>{x.terms} 条术语 · {x.episodes} 集</option>)}</datalist>
            <textarea className="inp" rows={3} style={{ marginTop: 8 }} placeholder="节目说明（会告诉大模型，例如：北海道渔村纪录片，大量老渔民方言采访）"
                      value={context} onChange={(e) => setContext(e.target.value)} />
          </div>
          <div>
            <h3>内容预设</h3>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              {presets.map((x) => (
                <div key={x.id} onClick={() => setPreset(x.id)} style={{ cursor: "pointer", border: `1px solid ${preset === x.id ? "var(--acc)" : "var(--line2)"}`,
                  background: preset === x.id ? "#ff7aa812" : "var(--bg2)", borderRadius: 8, padding: 10 }}>
                  <b>{x.name}</b><div className="dim" style={{ fontSize: 12 }}>{x.desc}</div>
                </div>
              ))}
            </div>
            <div className="dim" style={{ fontSize: 12, marginTop: 8 }}>这些设置都写在项目的 project.toml 里，之后可以在「流程」「图层样式」里随时改。</div>
          </div>
        </div>
        <div className="row" style={{ marginTop: 14, justifyContent: "flex-end" }}>
          <button className="btn ghost" onClick={onCancel}>取消</button>
          <button className="btn pri" disabled={busy || targets.length === 0} onClick={create}>{busy ? "创建中…" : "创建项目"}</button>
        </div>
      </div>
    </div>
  );
}
