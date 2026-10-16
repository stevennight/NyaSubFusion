import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useStore } from "../store";
import type { Cfg, StageStatus } from "../types";
import { deepGet, setIn, STAGE_LBL } from "../util";

// 步骤图：列 / 行位置与连线
const NODES: { id: string; c: number; r: number }[] = [
  { id: "probe", c: 1, r: 2 }, { id: "audio", c: 2, r: 1 }, { id: "subs", c: 2, r: 2 }, { id: "ocr", c: 2, r: 3 },
  { id: "asr", c: 3, r: 1 }, { id: "align", c: 3, r: 2 }, { id: "ocr_refine", c: 3, r: 3 }, { id: "fuse", c: 4, r: 2 },
  { id: "translate", c: 5, r: 2 }, { id: "annotate", c: 6, r: 2 }, { id: "render", c: 7, r: 2 },
];
const EDGES = [["probe", "audio"], ["probe", "subs"], ["probe", "ocr"], ["audio", "asr"], ["subs", "align"], ["asr", "align"],
  ["ocr", "ocr_refine"], ["ocr_refine", "fuse"], ["align", "fuse"], ["fuse", "translate"], ["translate", "annotate"], ["annotate", "render"]];
const STATUS_LBL: Record<string, string> = { done: "可复用", cached: "复用", stale: "将重做", missing: "未运行", running: "运行中", failed: "失败", waiting: "等待外部翻译" };

export function PipelinePage({ onReview }: { onReview: () => void }) {
  const s = useStore();
  const [draft, setDraft] = useState<Cfg>({});
  const [until, setUntil] = useState("render");
  const [redoAll, setRedoAll] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (s.config) setDraft(s.config.project); }, [s.config]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(s.config?.project ?? {});

  const status = useMemo(() => {
    const m: Record<string, StageStatus> = {};
    for (const p of s.summary?.plan ?? []) m[p.stage] = p.status;
    m.render = s.summary?.outputs.length && m.annotate === "done" ? "done" : "missing";
    return { ...m, ...s.live };
  }, [s.summary, s.live]);
  const seconds = Object.fromEntries((s.summary?.plan ?? []).map((p) => [p.stage, p.seconds]));
  const stale = (s.summary?.plan ?? []).filter((p) => p.status !== "done");

  const val = (path: string) => deepGet(draft, path) ?? deepGet(s.config?.effective, path);
  const set = (path: string, v: unknown) => setDraft((d) => setIn(d, path, v));

  const save = async () => {
    setSaving(true);
    try { await s.setConfig(draft); s.toast("配置已保存"); } catch (e) { s.setError((e as Error).message); } finally { setSaving(false); }
  };

  return (
    <div className="page"><div className="wide">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>流程</h2><span className="dim">改动任何参数，只重做受影响的步骤</span><span className="sp" />
        <select className="inp" style={{ width: "auto" }} value={until} onChange={(e) => setUntil(e.target.value)}>
          {["subs", "asr", "ocr", "fuse", "translate", "annotate", "render"].map((x) => <option key={x} value={x}>做到：{STAGE_LBL[x]}</option>)}
        </select>
        <label className="row dim"><input type="checkbox" checked={redoAll} onChange={(e) => setRedoAll(e.target.checked)} />全部重做</label>
        <button className="btn pri" disabled={s.running || dirty} title={dirty ? "先保存配置" : ""}
                onClick={() => s.run({ until, redo: redoAll ? ["all"] : [] })}>{s.running ? "运行中…" : "▶ 运行"}</button>
      </div>
      {stale.length > 0 && !s.running && (
        <div className="banner">⚠ 以下 {stale.length} 个步骤需要运行：{stale.map((p) => STAGE_LBL[p.stage]).join(" → ")}</div>
      )}
      <div className="card"><h3>步骤</h3><StageGraph status={status} seconds={seconds} /></div>
      <div className="gap" />

      <div className="row" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>配置</h3>
        <span className="dim" style={{ fontSize: 12 }}>保存在项目的 project.toml；没写的项用全局 / 作品配置</span>
        <span className="sp" />
        {dirty && <span className="chip warn">未保存</span>}
        <button className="btn" disabled={!dirty} onClick={() => setDraft(s.config?.project ?? {})}>还原</button>
        <button className="btn pri" disabled={!dirty || saving} onClick={save}>保存配置</button>
      </div>
      <div className="grid3">
        <div className="card">
          <h3>项目</h3>
          <div className="field"><label>源语言</label>
            <select className="inp" value={String(val("project.source"))} onChange={(e) => set("project.source", e.target.value)}>
              <option value="auto">自动判断</option>{s.langs.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
            </select></div>
          <div className="field"><label>目标语言（逗号分隔）</label>
            <input className="inp" value={((val("project.targets") as string[]) ?? []).join(",")}
                   onChange={(e) => set("project.targets", e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} /></div>
          <div className="field"><label>所属作品</label>
            <input className="inp" value={String(val("project.series") ?? "")} onChange={(e) => set("project.series", e.target.value)} /></div>
          <div className="field"><label>节目说明（会告诉大模型）</label>
            <textarea className="inp" rows={3} value={String(val("project.context") ?? "")} onChange={(e) => set("project.context", e.target.value)} /></div>
        </div>
        <div className="card">
          <h3>识别来源</h3>
          <label className="row" style={{ marginBottom: 6 }}><input type="checkbox" checked={!!val("subs.enabled")} onChange={(e) => set("subs.enabled", e.target.checked)} /><span className="badge b-c">字幕</span>使用自带字幕</label>
          <div className="field"><input className="inp" placeholder="指定字幕文件（空 = 自动找同名 / 内封字幕）" value={String(val("subs.path") ?? "")} onChange={(e) => set("subs.path", e.target.value)} /></div>
          <label className="row" style={{ marginBottom: 6 }}><input type="checkbox" checked={!!val("asr.enabled")} onChange={(e) => set("asr.enabled", e.target.checked)} /><span className="badge b-a">语音</span>语音识别</label>
          <div className="kv" style={{ marginBottom: 10 }}>
            <span className="lbl">模型</span>
            <select className="inp" value={String(val("asr.model"))} onChange={(e) => set("asr.model", e.target.value)}>
              {["large-v3", "large-v3-turbo", "medium", "small", "kotoba-tech/kotoba-whisper-v2.0-faster"].map((m) => <option key={m}>{m}</option>)}
            </select>
            <span className="lbl">人声门槛</span>
            <div className="row"><input type="range" min={0.1} max={0.6} step={0.05} value={Number(val("asr.vad_threshold"))} onChange={(e) => set("asr.vad_threshold", Number(e.target.value))} /><span className="dim">{String(val("asr.vad_threshold"))}</span></div>
          </div>
          <label className="row" style={{ marginBottom: 6 }}><input type="checkbox" checked={!!val("ocr.enabled")} onChange={(e) => set("ocr.enabled", e.target.checked)} /><span className="badge b-o">画面字</span>画面字识别</label>
          <div className="kv">
            <span className="lbl">扫描帧率</span>
            <div className="row"><input type="range" min={1} max={6} step={0.5} value={Number(val("ocr.fps"))} onChange={(e) => set("ocr.fps", Number(e.target.value))} /><span className="dim">{String(val("ocr.fps"))} fps</span></div>
            <span className="lbl">视觉复核</span>
            <div className="seg">{["none", "vlm"].map((v) => <button key={v} className={val("ocr.refine") === v ? "on" : ""} onClick={() => set("ocr.refine", v)}>{v === "none" ? "不复核" : "视觉大模型"}</button>)}</div>
          </div>
        </div>
        <div className="card">
          <h3>大模型</h3>
          <div className="field"><label>方式</label>
            <div className="seg">{[["auto", "自动"], ["api", "调接口"], ["export", "导出任务"], ["off", "不翻译"]].map(([v, l]) =>
              <button key={v} className={val("llm.mode") === v ? "on" : ""} onClick={() => set("llm.mode", v)}>{l}</button>)}</div></div>
          <div className="field"><label>接口地址（OpenAI 兼容）</label>
            <input className="inp" value={String(val("llm.base_url") ?? "")} onChange={(e) => set("llm.base_url", e.target.value)} /></div>
          <div className="field"><label>模型</label>
            <input className="inp" placeholder="例如 deepseek-chat" value={String(val("llm.model") ?? "")} onChange={(e) => set("llm.model", e.target.value)} /></div>
          <div className="field"><label>API Key 环境变量名</label>
            <input className="inp" value={String(val("llm.api_key_env") ?? "")} onChange={(e) => set("llm.api_key_env", e.target.value)} /></div>
          <div className="dim" style={{ fontSize: 12 }}>API Key 不写进项目文件：放在环境变量，或用户全局配置里。</div>
        </div>
      </div>
      <div className="gap" />
      <div className="grid2" style={{ gridTemplateColumns: "1.1fr .9fr" }}>
        <div className="card"><h3>融合结果</h3><Stats /><div className="gap" /><button className="btn" disabled={!s.data?.cues.length} onClick={onReview}>去审校 →</button></div>
        <div className="card"><h3>日志</h3><LogView lines={s.logs} /></div>
      </div>
    </div></div>
  );
}

function StageGraph({ status, seconds }: { status: Record<string, StageStatus>; seconds: Record<string, number | null | undefined> }) {
  const box = useRef<HTMLDivElement>(null);
  const [paths, setPaths] = useState<string[]>([]);
  useLayoutEffect(() => {
    const draw = () => {
      const el = box.current;
      if (!el) return;
      const R = el.getBoundingClientRect();
      setPaths(EDGES.map(([a, b]) => {
        const A = el.querySelector(`[data-n="${a}"]`)!.getBoundingClientRect();
        const B = el.querySelector(`[data-n="${b}"]`)!.getBoundingClientRect();
        const x1 = A.right - R.left, y1 = A.top + A.height / 2 - R.top, x2 = B.left - R.left - 2, y2 = B.top + B.height / 2 - R.top;
        const mx = (x1 + x2) / 2;
        return `M${x1} ${y1} C${mx} ${y1},${mx} ${y2},${x2} ${y2}`;
      }));
    };
    draw();
    window.addEventListener("resize", draw);
    return () => window.removeEventListener("resize", draw);
  }, []);
  return (
    <div className="dag" ref={box} style={{ gridTemplateColumns: "repeat(7, minmax(0, 1fr))" }}>
      <svg><defs><marker id="ar" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L8 4L0 8z" fill="#5a5a6c" /></marker></defs>
        {paths.map((d, i) => <path key={i} d={d} stroke="#4a4a5a" strokeWidth="1.5" fill="none" markerEnd="url(#ar)" />)}</svg>
      {NODES.map((n) => {
        const st = status[n.id] ?? "missing";
        return (
          <div key={n.id} data-n={n.id} className={`node ${st}`} style={{ gridColumn: n.c, gridRow: n.r }}>
            <span className="st" /><b>{STAGE_LBL[n.id]}</b>
            <small>{STATUS_LBL[st] ?? st}{seconds[n.id] ? ` · ${seconds[n.id]}s` : ""}</small>
          </div>
        );
      })}
    </div>
  );
}

function Stats() {
  const s = useStore();
  const st = s.data?.fused.stats;
  if (!st) return <div className="dim">还没有融合结果</div>;
  const cues = s.data!.cues;
  const todo = cues.filter((c) => c.review === "todo").length;
  return (
    <div className="stats">
      <div className="stat"><div className="n" style={{ color: "var(--c-sub)" }}>{st.subs}</div><div className="d">字幕条目</div></div>
      <div className="stat"><div className="n" style={{ color: "var(--c-ocr)" }}>{st.ocr_total}</div>
        <div className="d">画面字：台词 {st.telop} · 说明 {st.info} · 重复 {st.ocr_dup} · 丢弃 {st.ocr_logo + st.ocr_junk + st.ocr_scene}</div></div>
      <div className="stat"><div className="n" style={{ color: "var(--c-asr)" }}>{st.asr_total}</div>
        <div className="d">语音：无覆盖 {st.asr_uncovered} · 作为参考 {st.asr_hint}</div></div>
      <div className="stat"><div className="n" style={{ color: "var(--acc)" }}>{cues.length}</div><div className="d">最终字幕 · 待审校 {todo}</div></div>
    </div>
  );
}

function LogView({ lines }: { lines: string[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [lines]);
  return <div className="log" ref={ref}>{lines.length ? lines.join("\n") : "（运行后显示日志）"}</div>;
}
