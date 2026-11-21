// 处理：左边是步骤列表（就是原来的步骤图，竖着排），点一步，右边是这一步的设置、状态和上次的日志。改了自动保存。
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import { ModelPicker } from "../components/ModelPicker";
import { RegionEditor, type Region } from "../components/RegionEditor";
import { REDO_STAGES } from "../components/RunBar";
import { SeriesPicker } from "../components/SeriesPicker";
import { SaveChip, templateChange, useDoc, useDraft, useUnsaved, type ConfigDoc } from "../doc";
import { showVal, stepOf } from "../settingLabels";
import { useStore } from "../store";
import type { Cfg, Change, ModelOption, Provider, StageStatus } from "../types";
import { briefError, deepGet, langName, pct, setIn, STAGE_LBL } from "../util";

const STATUS_LBL: Record<string, string> = { pending: "待执行", done: "可复用", cached: "复用", stale: "要重做", missing: "未运行", running: "运行中",
  failed: "失败", waiting: "等外部翻译", cancelled: "已取消", off: "关", empty: "没有内容" };
const DOT: Record<string, string> = { done: "done", cached: "done", stale: "stale", missing: "stale", pending: "stale", running: "run", failed: "fail",
  waiting: "stale", cancelled: "stale", off: "off", empty: "off" };
const SEP_LBL: Record<string, string> = { auto: "自动", on: "开", off: "关" };
const ASR_API_KINDS = {
  transcribe: { label: "转写接口", model: "gpt-transcribe", desc: "OpenAI 兼容的 /v1/audio/transcriptions（gpt-transcribe、whisper-1 等）；只给文字的接口会切成小段识别" },
  gemini: { label: "多模态大模型", model: "gemini-2.5-flash", desc: "Gemini 等能听音频的大模型（chat/completions + 音频），按句给时间，没有逐字时间。会带上「资料」里的节目说明，和术语表里手动加的、导入的名字（汉字和读音），人名能写对，例如八犬伝的「四白（よしろ）」；自动整理的术语不带" },
};
const LLM_MODE: Record<string, string> = { auto: "自动", api: "调接口", export: "导出任务文件", off: "不翻译" };

type StepId = "basic" | "subs" | "asr" | "ocr" | "ocr_refine" | "align" | "fuse" | "organize" | "translate" | "annotate" | "render" | "video";
const GROUPS: { title: string; sub?: string; steps: StepId[] }[] = [
  { title: "开始", steps: ["basic"] },
  { title: "来源", sub: "识别出原文", steps: ["subs", "asr", "ocr", "ocr_refine"] },
  { title: "合成", sub: "合成一份字幕", steps: ["align", "fuse", "organize", "translate"] },
  { title: "输出", sub: "设置在「输出」页", steps: ["annotate", "render", "video"] },
];
const STEP_NAME: Record<StepId, string> = { ...STAGE_LBL, basic: "语言与作品", video: "生成视频" } as Record<StepId, string>;
// 每一步开关的是哪个设置（开 / 关两个值）
const TOGGLE: Partial<Record<StepId, [string, unknown, unknown]>> = {
  subs: ["subs.enabled", true, false], asr: ["asr.enabled", true, false], ocr: ["ocr.enabled", true, false], ocr_refine: ["ocr.refine", "vlm", "none"],
};

type ModelCache = Record<string, { loading?: boolean; loaded?: boolean; error?: string; models: ModelOption[] }>;
const DEFAULT_EFFORTS = ["low", "medium", "high", "xhigh"];
const AGY_SUFFIX_RE = /-(low|medium|high|xhigh|max)$/i;

function resolveAgyModel(model: string, effort: string, models?: ModelOption[]) {
  if (!model) return { actual: "", overridden: false, missing: false };
  const availIds = new Set((models ?? []).map((m) => m.id));
  const eff = effort.trim().toLowerCase();
  if (eff) {
    const base = model.replace(AGY_SUFFIX_RE, "");
    const cand = `${base}-${eff}`;
    if (availIds.size > 0) {
      if (availIds.has(cand)) return { actual: cand, overridden: cand !== model && AGY_SUFFIX_RE.test(model), missing: false };
      return { actual: model, overridden: false, missing: true };
    }
    return { actual: cand, overridden: cand !== model && AGY_SUFFIX_RE.test(model), missing: false };
  }
  if (availIds.size > 0 && !availIds.has(model)) {
    const pref = `${model}-`;
    for (const suf of ["medium", "high", "low"]) {
      if (availIds.has(pref + suf)) return { actual: pref + suf, overridden: false, missing: false };
    }
    const first = [...availIds].sort().find((id) => id.startsWith(pref));
    if (first) return { actual: first, overridden: false, missing: false };
  }
  return { actual: model, overridden: false, missing: false };
}

function EffortField({
  value,
  onChange,
  kind,
  effectiveModel,
  models,
  tip,
}: {
  value: string;
  onChange: (v: string) => void;
  kind?: string;
  effectiveModel?: string;
  models?: ModelOption[];
  tip?: string;
}) {
  const listId = useId();
  const base = (effectiveModel ?? "").replace(AGY_SUFFIX_RE, "");
  const matched = models?.find((m) => m.id === effectiveModel)
    ?? (base ? models?.find((m) => m.id.replace(AGY_SUFFIX_RE, "") === base && (m.efforts?.length ?? 0) > 0) : undefined);
  const opts = matched?.efforts?.length ? matched.efforts : DEFAULT_EFFORTS;
  const preview = kind === "agy" && effectiveModel ? resolveAgyModel(effectiveModel, value, models) : null;
  return (
    <div className="row">
      <input
        className="inp"
        list={listId}
        style={{ maxWidth: 160 }}
        placeholder="不设（默认）"
        value={value}
        onChange={(e) => onChange(e.target.value.trim())}
      />
      <datalist id={listId}>{opts.map((x) => <option key={x} value={x} />)}</datalist>
      {value && <button type="button" className="btn sm ghost" title="清空思考程度（改回默认）" onClick={() => onChange("")}>×</button>}
      {tip && <Tip text={tip} />}
      {preview && (
        preview.missing
          ? <span className="hint" style={{ color: "var(--warn)" }}>该模型无 {value} 档，将回退到 {preview.actual || effectiveModel}</span>
          : value
            ? <span className="hint">实际调用：<code>{preview.actual}</code>{preview.overridden ? "（已覆盖模型后缀）" : ""}</span>
            : <span className="hint">留空 = 按模型自带档位（<code>{preview.actual}</code>）</span>
      )}
    </div>
  );
}

export function ProcessPage({ onTab }: { onTab: (t: "review" | "output" | "notes") => void }) {
  const s = useStore();
  const doc = useDoc();
  // 草稿就是这一份设置；保存时只把改过的项写回去（别的页面同时改了别的项也不会被冲掉）
  const touched = useRef(new Set<string>());
  const { v: draft, update, state } = useDraft<Cfg>(doc, (d) => structuredClone(d.raw),
    (raw, v) => [...touched.current].reduce((out, p) => setIn(out, p, deepGet(v, p)), raw));
  const [step, setStep] = useState<StepId>(() => (localStorage.getItem("nyasub.step") as StepId) || "basic");
  const [modelLists, setModelLists] = useState<ModelCache>({});
  useEffect(() => { localStorage.setItem("nyasub.step", step); }, [step]);
  const episode = doc?.kind === "episode";

  const status = useMemo(() => {
    const m: Record<string, StageStatus> = {};
    for (const p of s.summary?.plan ?? []) m[p.stage] = p.status;
    m.render = s.summary?.render?.status ?? "missing";
    const live = Object.fromEntries(Object.entries(s.live).filter(([k, v]) => !((m[k] === "off" || m[k] === "empty") && v !== "failed")));
    return { ...m, ...live };
  }, [s.summary, s.live]);
  if (!doc || !draft) return <div className="empty">加载中…</div>;
  const eff = doc.effective;
  const rawVal = (path: string) => deepGet(draft, path);
  const val = (path: string) => deepGet(draft, path) ?? deepGet(eff, path);
  const set = (path: string, value: unknown) => { touched.current.add(path); update((d) => setIn(d, path, value)); };
  const modelKey = (p?: Provider, legacyBase = "") =>
    p ? `${p.name}|${p.kind ?? "openai"}|${p.base_url}|${p.command ?? ""}` : (legacyBase ? `__legacy__|${legacyBase}` : "__none__");
  const fetchModels = async (p?: Provider, legacyBase = "", blockReason = "") => {
    const k = modelKey(p, legacyBase);
    setModelLists((m) => ({ ...m, [k]: { ...(m[k] ?? { models: [] }), loading: true, error: "" } }));
    if (blockReason) {
      setModelLists((m) => ({ ...m, [k]: { loading: false, loaded: true, error: blockReason, models: [] } }));
      return;
    }
    if (!p && !legacyBase) {
      setModelLists((m) => ({ ...m, [k]: { loading: false, loaded: true, error: "请先在「设置」中添加可用接口", models: [] } }));
      return;
    }
    try {
      const pk = p?.kind ?? "openai";
      const r = await api.providerModels(p
        ? (pk === "agy" || pk === "codex"
          ? { name: p.name, kind: pk, command: p.command ?? "" }
          : { name: p.name, kind: "openai", base_url: p.base_url, api_key_env: p.api_key_env ?? "" })
        : { kind: "openai", base_url: legacyBase });
      setModelLists((m) => ({
        ...m,
        [k]: { loading: false, loaded: true, error: r.ok ? "" : (r.error || "读取失败"), models: r.models ?? [] },
      }));
    } catch (e) {
      setModelLists((m) => ({ ...m, [k]: { ...(m[k] ?? { models: [] }), loading: false, loaded: true, error: (e as Error).message } }));
    }
  };
  const seconds = (st: string) => (st in s.liveSec ? s.liveSec[st] : ["pending", "running"].includes(status[st]) ? null
    : s.summary?.plan.find((p) => p.stage === st)?.seconds);
  const note = (st: string) => (st === "render" ? s.summary?.render?.note : s.summary?.plan.find((p) => p.stage === st)?.note);
  const vmode = String(val("render.video.mode") ?? "off");
  const stepStatus = (id: StepId): string => (id === "basic" ? "" : id === "video" ? (vmode === "off" ? "off" : status[vmode] ?? "missing") : status[id] ?? "missing");
  const changes = doc.template?.changes ?? [];
  const diffs = (id: StepId) => changes.filter((c) => stepOf(c.keys) === id);
  const ctx: StepCtx = { doc, val, rawVal, set, modelLists, modelKey, fetchModels, episode, onTab, status: stepStatus, note, seconds, diffs };

  return (
    <div className="page"><div className="wide">
      <div className="md">
        <div className="steps">
          <div className="row" style={{ padding: "2px 6px 4px" }}><span className="grp" style={{ padding: 0 }}>步骤</span><span className="sp" /><SaveChip state={state} /></div>
          {GROUPS.map((g) => (
            <div key={g.title}>
              <div className="grp">{g.title}{g.sub && <span className="dim"> · {g.sub}</span>}</div>
              {g.steps.map((id) => <StepRow key={id} id={id} ctx={ctx} sel={step === id} onClick={() => setStep(id)} />)}
            </div>
          ))}
        </div>
        <div className="detail">
          <StepDetail id={step} ctx={ctx} />
        </div>
      </div>
    </div></div>
  );
}

interface StepCtx {
  doc: ConfigDoc;
  val: (p: string) => unknown;
  rawVal: (p: string) => unknown;
  set: (p: string, v: unknown) => void;
  modelLists: ModelCache;
  modelKey: (p?: Provider, legacyBase?: string) => string;
  fetchModels: (p?: Provider, legacyBase?: string, blockReason?: string) => Promise<void>;
  episode: boolean;
  onTab: (t: "review" | "output" | "notes") => void;
  status: (id: StepId) => string;
  note: (st: string) => string | undefined;
  seconds: (st: string) => number | null | undefined;
  diffs: (id: StepId) => Change[];   // 这一步里和模板不一样的项
}

/** 设置项的标签：和模板不一样时标紫，旁边一个 ↺ 只把这一项恢复成模板的值 */
function Lbl({ ctx, k, children }: { ctx: StepCtx; k: string | string[]; children: ReactNode }) {
  const unsaved = useUnsaved();
  const ch = (Array.isArray(k) ? k : [k]).map((p) => templateChange(ctx.doc, p.split("."))).filter(Boolean) as Change[];
  if (!ch.length || !ctx.episode) return <span className="lbl">{children}</span>;
  return (
    <span className="lbl mod" title={`和模板不一样，模板里是：${ch.map((c) => showVal(c.after)).join(" / ")}`}>
      {children}
      <button className="rv" disabled={unsaved} title={`恢复成模板的值：${ch.map((c) => showVal(c.after)).join(" / ")}`}
              onClick={() => ctx.doc.applyTemplate?.(ch.map((c) => c.keys), "episode")}>↺</button>
    </span>
  );
}

function summary(id: StepId, val: (p: string) => unknown, langs: { code: string; name: string }[]): string {
  const ln = (c: string) => (c === "auto" ? "自动判断" : langName(c, langs));
  switch (id) {
    case "basic": return `${ln(String(val("project.source") ?? "auto"))} → ${((val("project.targets") as string[]) ?? []).map(ln).join("、") || "—"}${val("project.series") ? ` · ${val("project.series")}` : ""}`;
    case "subs": return val("subs.path") ? `指定：${String(val("subs.path")).split(/[\\/]/).pop()}` : "自动找同名 / 内封字幕";
    case "asr": return val("asr.backend") === "api" ? `云端 · ${String(val("asr.api_model") || "默认模型")}` : `本机 · ${String(val("asr.model"))} · 人声分离${SEP_LBL[String(val("asr.separate") ?? "auto")]}`;
    case "ocr": return `${String(val("ocr.fps"))} 帧/秒 · ${((val("ocr.regions") as unknown[]) ?? []).length} 个区域`;
    case "ocr_refine": return val("ocr.refine") === "vlm" ? (val("ocr.refine_mode") === "crop" ? "看本地截图逐处确认" : "看整帧画面逐处确认") : "关 · 用视觉大模型逐处确认";
    case "align": return "按语音校准字幕时间";
    case "fuse": return "几路结果合成一份";
    case "organize": return `合并来源、分类、丢杂字 · 只出原文${val("subs.skip_sound_annotations") ? " · 跳过声音说明" : ""}`;
    case "translate": {
      const effort = String(val("llm.reasoning_effort") ?? "");
      const how = `${val("llm.grouped") ? " · 分组翻" : ""}${effort ? ` · 思考 ${effort}` : ""}`;
      return `${LLM_MODE[String(val("llm.mode") ?? "auto")]}${how}${val("llm.frames") ? " · 附画面" : ""}${val("llm.listen_audio") ? " · 听原声" : ""}`;
    }
    case "annotate": {
      const n = ((val("render.profiles") as { layers: { annotate?: { kind: string } }[] }[]) ?? []).flatMap((p) => p.layers).filter((l) => l.annotate && l.annotate.kind !== "none").length;
      return n ? `${n} 个图层有注音` : "没有图层加注音";
    }
    case "render": return `${((val("render.profiles") as { enabled?: boolean }[]) ?? []).filter((p) => p.enabled !== false).length} 个字幕文件`;
    case "video": { const m = String(val("render.video.mode") ?? "off"); return m === "mux" ? "运行完自动封装 MKV" : m === "burn" ? "运行完自动压制硬字幕" : "不自动生成"; }
  }
}

function StepRow({ id, ctx, sel, onClick }: { id: StepId; ctx: StepCtx; sel: boolean; onClick: () => void }) {
  const s = useStore();
  const st = ctx.status(id);
  const tg = TOGGLE[id];
  const on = tg ? ctx.val(tg[0]) === tg[1] : undefined;
  const redo = REDO_STAGES.includes(id) && (s.runOpts.redo.includes("all") || s.runOpts.redo.includes(id));
  const p = st === "running" ? s.stageProg[id] : undefined;
  const sec = ctx.seconds(id);
  return (
    <div className={`step${sel ? " sel" : ""}${on === false ? " offs" : ""}`} onClick={onClick}>
      <span className={`dot ${ctx.episode && st ? DOT[st] ?? "na" : "na"}`} />
      <div className="nm">
        <b>{STEP_NAME[id]}{["annotate", "render", "video"].includes(id) && <span className="dim"> ↗</span>}{redo && <span className="chip mini warn" title="下次运行一定重做">↻</span>}
          {ctx.episode && ctx.diffs(id).length > 0 && <span className="moddot" title={`和模板不一样：${ctx.diffs(id).length} 项`}>●</span>}</b>
        <small>{p && p.total > 0 ? `${p.label || "运行中"} ${pct(p)}%` : summary(id, ctx.val, s.langs)}</small>
      </div>
      {tg ? <button className={`sw${on ? " on" : ""}`} title={on ? "开（点一下关掉）" : "关（点一下打开）"} onClick={(e) => { e.stopPropagation(); ctx.set(tg[0], on ? tg[2] : tg[1]); }} />
        : ctx.episode && st ? <span className="dim st-lbl">{st === "done" && sec ? `${sec}s` : STATUS_LBL[st] ?? ""}</span> : null}
    </div>
  );
}

// ---------------- 每一步 ----------------
function StepDetail({ id, ctx }: { id: StepId; ctx: StepCtx }) {
  const body: Record<StepId, () => ReactNode> = {
    basic: () => <BasicStep ctx={ctx} />, subs: () => <SubsStep ctx={ctx} />, asr: () => <AsrStep ctx={ctx} />, ocr: () => <OcrStep ctx={ctx} />,
    ocr_refine: () => <RefineStep ctx={ctx} />, align: () => <Plain text="按语音识别的时间校准自带字幕的时间（广播字幕常整体偏移 1~2 秒）。没有可调的设置。" />,
    fuse: () => <><Plain text="把字幕、语音、画面字三路结果按时间合成一份：去重、分出台词和说明、补上字幕漏掉的话。没有可调的设置。" />{ctx.episode && <FuseStats />}</>,
    organize: () => <OrganizeStep ctx={ctx} />, translate: () => <TranslateStep ctx={ctx} />, annotate: () => <JumpStep ctx={ctx} text="注音是按图层设的：在「输出」里选一个字幕文件，点开图层就能设注音（振假名、拼音……）。读音不对的写进「资料」里的读音词典。" />,
    render: () => <JumpStep ctx={ctx} text="输出哪些字幕文件、每个文件里有哪些图层、用什么样式，都在「输出」里。" />,
    video: () => <JumpStep ctx={ctx} text="封装 MKV、压制硬字幕，以及运行完要不要自动做，都在「输出」里。" />,
  };
  return (
    <div className="col">
      <StepHead id={id} ctx={ctx} />
      {body[id]()}
      {ctx.episode && id !== "basic" && <LastRun stage={id === "video" ? String(ctx.val("render.video.mode")) : id} ctx={ctx} />}
    </div>
  );
}

function StepHead({ id, ctx }: { id: StepId; ctx: StepCtx }) {
  const s = useStore();
  const st = ctx.status(id);
  const note = ctx.note(id);
  const canRedo = REDO_STAGES.includes(id) && st !== "off";
  const all = s.runOpts.redo.includes("all");
  const redo = canRedo && (all || s.runOpts.redo.includes(id));
  const tg = TOGGLE[id];
  const unsaved = useUnsaved();
  const diffs = ctx.episode ? ctx.diffs(id) : [];
  return (
    <div className="dh">
      <h2>{STEP_NAME[id]}</h2>
      {ctx.episode && st && <span className={`chip ${DOT[st] === "done" ? "ok" : DOT[st] === "fail" ? "bad" : DOT[st] === "stale" ? "warn" : ""}`}>{STATUS_LBL[st] ?? st}</span>}
      {ctx.episode && note && <span className="hint">{note}</span>}
      <span className="sp" />
      {diffs.length > 0 && <button className="btn sm tplb" disabled={unsaved} title={`这一步有 ${diffs.length} 项和模板不一样，全部恢复成模板的值（先备份）`}
                                   onClick={() => ctx.doc.applyTemplate?.(diffs.map((c) => c.keys), "episode")}>↺ 这一步恢复成模板</button>}
      {tg && <label className="row dim"><button className={`sw${ctx.val(tg[0]) === tg[1] ? " on" : ""}`} onClick={() => ctx.set(tg[0], ctx.val(tg[0]) === tg[1] ? tg[2] : tg[1])} />{ctx.val(tg[0]) === tg[1] ? "开" : "关"}</label>}
      {ctx.episode && canRedo && !s.running && (
        <button className={`btn sm${redo ? " warn" : ""}`} title={redo ? "已勾选：下次运行一定重做这一步（再点取消）" : "下次运行一定重做这一步（平时不用：改了设置会自动重做）"}
                onClick={() => s.setRunOpts((o) => ({ ...o, redo: all ? REDO_STAGES.filter((x) => x !== id) : redo ? o.redo.filter((x) => x !== id) : [...o.redo, id] }))}>
          ↻ {redo ? "下次会重做" : "下次重做这一步"}</button>
      )}
    </div>
  );
}

function Plain({ text }: { text: string }) {
  return <div className="card"><div className="hint">{text}</div></div>;
}

function JumpStep({ ctx, text }: { ctx: StepCtx; text: string }) {
  return <div className="card"><div className="hint" style={{ marginBottom: 10 }}>{text}</div><button className="btn" onClick={() => ctx.onTab("output")}>去「输出」设置 →</button></div>;
}

function Adv({ children }: { children: ReactNode }) {
  return <details className="adv"><summary>高级</summary>{children}</details>;
}

function Tip({ text }: { text: string }) {
  return <span className="tip" title={text}>ⓘ</span>;
}

function Seg({ value, options, onChange }: { value: string; options: [string, string][]; onChange: (v: string) => void }) {
  return <div className="seg">{options.map(([v, l]) => <button key={v} className={value === v ? "on" : ""} onClick={() => onChange(v)}>{l}</button>)}</div>;
}

function BasicStep({ ctx }: { ctx: StepCtx }) {
  const s = useStore();
  const { val, set } = ctx;
  const m = s.summary?.meta;
  const targets = (val("project.targets") as string[]) ?? [];
  return (
    <div className="card">
      <div className="kv">
        <Lbl ctx={ctx} k="project.source">源语言</Lbl>
        <select className="inp" style={{ maxWidth: 220 }} value={String(val("project.source"))} onChange={(e) => set("project.source", e.target.value)}>
          <option value="auto">自动判断</option>{s.langs.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
        </select>
        <Lbl ctx={ctx} k="project.targets">翻译成</Lbl>
        <div className="row wrap">
          {s.langs.map((l) => {
            const on = targets.includes(l.code);
            return <span key={l.code} className={`chip ${on ? "acc" : ""}`} style={{ cursor: "pointer" }} title={on && targets[0] === l.code ? "第一个目标语言：融合和翻译以它为准" : ""}
                         onClick={() => set("project.targets", on ? targets.filter((t) => t !== l.code) : [...targets, l.code])}>
              {on && targets.length > 1 ? `${targets.indexOf(l.code) + 1}. ` : ""}{l.name}</span>;
          })}
        </div>
        {ctx.episode && (<>
          <span className="lbl">所属作品</span>
          <div className="row"><div style={{ maxWidth: 320, flex: 1 }}><SeriesPicker value={String(val("project.series") ?? "")} onChange={(v) => set("project.series", v)} /></div>
            <span className="hint">同一作品的各集共用术语表、读音词典</span></div>
          <span className="lbl">视频</span>
          <div className="muted" style={{ wordBreak: "break-all" }}>{s.summary?.video}{m ? ` · ${m.video ? `${m.video.width}×${m.video.height} · ` : "纯音频 · "}${Math.floor(m.duration / 60)} 分 ${Math.round(m.duration % 60)} 秒` : ""}</div>
        </>)}
      </div>
    </div>
  );
}

function SubsStep({ ctx }: { ctx: StepCtx }) {
  const { val, set } = ctx;
  return (
    <div className="card">
      <div className="kv">
        <span className="lbl">用哪条字幕</span>
        <input className="inp" placeholder="空 = 自动找同名外挂字幕 / 内封字幕；也可以写字幕文件的路径" value={String(val("subs.path") ?? "")} onChange={(e) => set("subs.path", e.target.value)} />
        <span /><span className="hint">最可信的来源：有字幕时以它为准，语音和画面字用来补漏、对时间。来自电视广播时还能输出“广播字幕原样”（在「输出」里）。</span>
      </div>
    </div>
  );
}

const providerOpt = (p: Provider, vision = false) => {
  const tag = p.kind === "agy" ? " [agy]" : p.kind === "codex" ? " [codex]" : "";
  const m = vision ? (p.vision_model || p.model) : p.model;
  return `${p.name}${tag}${m ? ` · ${m}` : ""}`;
};

function AsrStep({ ctx }: { ctx: StepCtx }) {
  const { val, set, doc, modelLists, modelKey, fetchModels } = ctx;
  const [models, setModels] = useState<{ id: string; label: string; desc: string }[]>([]);
  useEffect(() => { api.asrModels().then(setModels).catch(() => {}); }, []);
  const backend = String(val("asr.backend") ?? "local");
  const kind = (String(val("asr.api_kind") ?? "transcribe") in ASR_API_KINDS ? String(val("asr.api_kind") ?? "transcribe") : "transcribe") as keyof typeof ASR_API_KINDS;
  const allProviders = (deepGet(doc.effective, "llm.providers") as Provider[] | undefined) ?? [];
  const providers = allProviders.filter((p) => !p.kind || p.kind === "openai" || p.kind === "agy");
  const translateName = String(deepGet(doc.effective, "llm.translate") ?? "");
  const asrProviderName = String(val("asr.api_provider") ?? "").trim() || String(deepGet(doc.effective, "llm.asr") ?? "").trim() || translateName;
  const asrProvider = allProviders.find((p) => p.name === asrProviderName);
  const asrKind = asrProvider?.kind ?? "openai";
  const legacyBase = String(deepGet(doc.effective, "llm.base_url") ?? "");
  const cacheKey = modelKey(asrProvider, legacyBase);
  const apiFetched = modelLists[cacheKey];
  const onFetch = () => fetchModels(
    asrProvider,
    legacyBase,
    asrProvider && asrKind === "codex" ? "当前翻译接口为 Codex CLI（不支持音频），请在上方选 OpenAI 兼容接口或 Antigravity CLI" : "",
  );
  const timingProviderName = String(val("asr.api_timing_provider") ?? "").trim();
  const timingLocal = timingProviderName === "local";
  const timingProviderObj = timingLocal ? undefined : (timingProviderName ? allProviders.find((p) => p.name === timingProviderName) : asrProvider);
  const timingKind = timingProviderObj?.kind ?? "openai";
  const timingKey = modelKey(timingProviderObj, legacyBase);
  const timingFetched = modelLists[timingKey];
  const onFetchTiming = () => fetchModels(
    timingProviderObj,
    legacyBase,
    timingKind !== "openai" ? "对时需 OpenAI 兼容的转写接口（或将对时接口选为「本机 faster-whisper」）" : "",
  );
  const setKind = (k: keyof typeof ASR_API_KINDS) => {
    set("asr.api_kind", k);
    const m = String(val("asr.api_model") ?? "");
    if (!m || Object.values(ASR_API_KINDS).some((x) => x.model === m)) set("asr.api_model", ASR_API_KINDS[k].model);
  };
  const cur = models.find((m) => m.id === val("asr.model"));
  const effectiveAsrModel = String(val("asr.api_model") || ASR_API_KINDS[kind].model);
  return (
    <div className="card">
      <div className="kv">
        <Lbl ctx={ctx} k="asr.backend">识别方式</Lbl>
        <div className="row"><Seg value={backend} options={[["local", "本机"], ["api", "云端接口"]]} onChange={(v) => set("asr.backend", v)} />
          <span className="hint">{backend === "api" ? "按音频时长付费，一小时节目约 0.2~0.4 美元" : "faster-whisper，免费，要显卡才快"}</span></div>
        {backend === "api" ? (<>
          <Lbl ctx={ctx} k="asr.api_provider">接口</Lbl>
          <select className="inp" value={String(val("asr.api_provider") ?? "")} onChange={(e) => set("asr.api_provider", e.target.value)}>
            <option value="">和翻译用同一个{translateName ? `（${translateName}）` : ""}</option>
            {providers.map((p) => <option key={p.name} value={p.name}>{p.kind === "agy" ? `${p.name} [agy]` : p.name}</option>)}
          </select>
          <Lbl ctx={ctx} k="asr.api_kind">接口类型</Lbl>
          <div className="row"><div className="seg">{(Object.keys(ASR_API_KINDS) as (keyof typeof ASR_API_KINDS)[]).map((k) =>
            <button key={k} className={kind === k ? "on" : ""} onClick={() => setKind(k)}>{ASR_API_KINDS[k].label}</button>)}</div><Tip text={ASR_API_KINDS[kind].desc} />
            {asrKind === "agy" && kind === "transcribe" && <span className="hint" style={{ color: "var(--warn)" }}>Antigravity CLI 仅支持「多模态大模型」模式，请切到右侧</span>}
          </div>
          <Lbl ctx={ctx} k="asr.api_model">模型</Lbl>
          <div className="row">
            <ModelPicker
              value={String(val("asr.api_model") ?? "")}
              placeholder={ASR_API_KINDS[kind].model}
              filterModality="audio"
              models={apiFetched?.models}
              loading={apiFetched?.loading}
              error={apiFetched?.error}
              loaded={apiFetched?.loaded}
              onFetch={onFetch}
              onChange={(v) => set("asr.api_model", v)}
            />
            <Tip text="要选给时间戳的模型。OpenRouter 可以填 mistralai/voxtral-mini-transcribe-3-0；用 Antigravity CLI 可选 gemini-4-argon-medium / gemini-4-hydrogen-medium 等 Gemini 模型" />
          </div>
          {kind === "gemini" && (<>
            <Lbl ctx={ctx} k="asr.api_effort">思考程度</Lbl>
            <EffortField
              value={String(val("asr.api_effort") ?? "")}
              onChange={(v) => set("asr.api_effort", v)}
              kind={asrKind}
              effectiveModel={effectiveAsrModel}
              models={apiFetched?.models}
              tip="多模态大模型听音转写时的思考程度（空 = 使用模型/接口默认）。通常 low 或不设即可。"
            />
            <Lbl ctx={ctx} k="asr.api_timing_provider">对时接口</Lbl>
            <div className="row">
              <select className="inp" style={{ maxWidth: 320 }} value={timingProviderName} onChange={(e) => set("asr.api_timing_provider", e.target.value)}>
                <option value="">和识别用同一个{asrProviderName ? `（${asrProviderName}）` : ""}</option>
                <option value="local">本机 faster-whisper（免费 · 借 GPU 逐字时间）</option>
                {allProviders.filter((p) => !p.kind || p.kind === "openai").map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
              </select>
              <Tip text="大模型听得准（人名、方言、整句意思），但时间是估的，常差几秒。选「本机 faster-whisper」可免费用显卡跑逐字时间来对齐大模型文字；也可单独选一个 OpenAI 兼容的转写接口（如 OpenRouter / OpenAI），不再受主识别接口限制。" />
            </div>
            {timingLocal ? (
              <><span /><div className="hint">使用本机 faster-whisper（{String(val("asr.model") || "large-v3")}）提取逐字时间，对齐上面多模态大模型识别出的精准文字。</div></>
            ) : (<>
              <Lbl ctx={ctx} k="asr.api_timing_model">对时用</Lbl>
              <div className="row">
                <ModelPicker
                  value={String(val("asr.api_timing_model") ?? "")}
                  placeholder="可选：转写模型，比如 microsoft/mai-transcribe-2"
                  filterModality="audio"
                  allowClear
                  models={timingFetched?.models}
                  loading={timingFetched?.loading}
                  error={timingFetched?.error}
                  loaded={timingFetched?.loaded}
                  onFetch={onFetchTiming}
                  onChange={(v) => set("asr.api_timing_model", v.trim())}
                />
                <Tip text="填一个转写模型时，会再用它识别一遍：字用大模型的，句子的起止对到转写模型的逐字时间上，大模型漏掉的短感叹（「え？」「うわあ！」）也补上。八犬伝第 1 集对字幕组的人工时间轴：开头差半秒以上的从 48% 降到 18%。多花一次转写模型的钱（mai-transcribe 很便宜）。" />
                {timingKind === "agy" && <span className="hint" style={{ color: "var(--warn)" }}>Antigravity CLI 不支持转写对时接口（可上方改选「本机 faster-whisper」或 OpenAI 兼容接口）</span>}
              </div>
            </>)}
          </>)}
        </>) : (<>
          <Lbl ctx={ctx} k="asr.model">模型</Lbl>
          <div className="row">
            <select className="inp" value={String(val("asr.model"))} onChange={(e) => set("asr.model", e.target.value)}>
              {(models.length ? models : [{ id: String(val("asr.model")), label: String(val("asr.model")), desc: "" }]).map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
              {models.length > 0 && !cur && <option value={String(val("asr.model"))}>{String(val("asr.model"))}（自定义）</option>}
            </select>
            {cur?.desc && <Tip text={cur.desc} />}
          </div>
        </>)}
        <Lbl ctx={ctx} k="asr.separate">人声分离</Lbl>
        <div className="row"><Seg value={String(val("asr.separate") ?? "auto")} options={Object.entries(SEP_LBL)} onChange={(v) => set("asr.separate", v)} />
          <span className="hint">先去掉背景音乐再识别；自动 = 没有字幕时才做</span></div>
      </div>
      <Adv>
        <div className="kv">
          {backend === "api" ? (<>
            <Lbl ctx={ctx} k="asr.api_chunk_seconds">每块时长</Lbl>
            <div className="row">
              <select className="inp" value={String(Number(val("asr.api_chunk_seconds") ?? 0))} onChange={(e) => set("asr.api_chunk_seconds", Number(e.target.value))}>
                <option value="0">自动（OpenRouter / agy 3 分钟，其它 10 分钟）</option>
                {[1, 2, 3, 5, 10, 20].map((m) => <option key={m} value={String(m * 60)}>{m} 分钟</option>)}
              </select>
              <Tip text="音频切成多长一块上传。太长可能超过接口的处理时间上限（OpenRouter 是 60 秒）而失败；太短切点多，句子被切断的机会多一点" />
            </div>
          </>) : (<>
            <Lbl ctx={ctx} k="asr.vad_threshold">人声门槛</Lbl>
            <div className="row"><input type="range" min={0.1} max={0.6} step={0.05} value={Number(val("asr.vad_threshold"))} onChange={(e) => set("asr.vad_threshold", Number(e.target.value))} />
              <span className="dim">{String(val("asr.vad_threshold"))}</span>
              <Tip text="多大的声音算有人说话。调低：小声、远处的话不容易漏，但杂音也可能被当成台词；调高则相反。" /></div>
          </>)}
        </div>
      </Adv>
    </div>
  );
}

function OcrStep({ ctx }: { ctx: StepCtx }) {
  const s = useStore();
  const { val, set } = ctx;
  const fps = Number(val("ocr.fps") || 1);
  return (
    <div className="card">
      <div className="kv">
        <Lbl ctx={ctx} k="ocr.fps">每秒看几帧</Lbl>
        <div className="row"><input type="range" min={1} max={10} step={0.5} value={fps} onChange={(e) => set("ocr.fps", Number(e.target.value))} />
          <span className="dim">{fps} 帧/秒 · 时间误差约 ±{(0.5 / fps).toFixed(2)} 秒</span>
          <Tip text="越大，画面字出现 / 消失的时间越准、越不容易漏掉一闪而过的字，但扫描越慢。台词类画面字建议 2~3；片尾滚动字幕、跑马灯可以调到 6~10。" /></div>
        <Lbl ctx={ctx} k="ocr.regions">识别区域</Lbl>
        {ctx.episode && s.pid && s.summary?.meta?.video ? (
          <div className="src-region"><RegionEditor pid={s.pid} duration={s.summary.meta.duration} aspect={s.summary.meta.video.aspect}
            regions={(val("ocr.regions") as Region[]) ?? [{ rect: [0, 0, 1, 1] }]} boxes={s.data?.ocr ?? []} onChange={(r) => set("ocr.regions", r)} /></div>
        ) : <span className="hint">{ctx.episode ? "纯音频，没有画面" : `${((val("ocr.regions") as unknown[]) ?? []).length} 个区域（在一集里对着画面框选）`}</span>}
      </div>
    </div>
  );
}

function RefineStep({ ctx }: { ctx: StepCtx }) {
  const { val, rawVal, set, doc, modelLists, modelKey, fetchModels } = ctx;
  const on = val("ocr.refine") === "vlm";
  const crop = val("ocr.refine_mode") === "crop";
  const providers = (deepGet(doc.effective, "llm.providers") as Provider[] | undefined) ?? [];
  const rawName = String(rawVal("llm.vision") ?? "").trim();
  const effName = String(deepGet(doc.effective, "llm.vision") ?? "").trim();
  const trName = String(val("llm.translate") ?? "").trim();
  const visName = rawName || effName || trName;
  const visProvider = providers.find((p) => p.name === visName);
  const legacyBase = String(deepGet(doc.effective, "llm.base_url") ?? "");
  const mKey = modelKey(visProvider, legacyBase);
  const fetched = modelLists[mKey];
  const defaultModel = visProvider ? (visProvider.vision_model || visProvider.model) : String(deepGet(doc.effective, "llm.vision_model") || deepGet(doc.effective, "llm.model") || "");
  const effectiveModel = String(val("llm.refine_model") || defaultModel).trim();
  return (
    <div className="card">
      <div className="hint" style={{ marginBottom: 10 }}>视觉大模型重新看画面字，纠正错字，并判断是后期叠加的字幕还是实物上的字（招牌、瓶子标签…）。可以和「翻译」分开选不同接口、模型与思考程度。</div>
      <div className="kv" style={on ? undefined : { opacity: 0.45, pointerEvents: "none" }}>
        <Lbl ctx={ctx} k="llm.vision">接口</Lbl>
        <div className="row"><select className="inp" value={rawName} onChange={(e) => set("llm.vision", e.target.value || undefined)}>
          <option value="">跟随默认{effName ? `（${effName}）` : trName ? `（同翻译：${trName}）` : ""}</option>{providers.map((p) => <option key={p.name} value={p.name}>{providerOpt(p, true)}</option>)}</select>
          <span className="hint">可单独指定 OpenAI 兼容接口或本机 CLI</span></div>
        <Lbl ctx={ctx} k="llm.refine_model">模型</Lbl>
        <div className="row">
          <ModelPicker
            value={String(val("llm.refine_model") ?? "")}
            placeholder={defaultModel ? `默认：${defaultModel}` : "可留空（用接口视觉/默认模型）"}
            filterModality="image"
            allowClear
            models={fetched?.models}
            loading={fetched?.loading}
            error={fetched?.error}
            loaded={fetched?.loaded}
            onFetch={() => fetchModels(visProvider, legacyBase)}
            onChange={(v) => set("llm.refine_model", v)}
          />
          <Tip text="当前作品或剧集覆盖复核模型；留空时使用全局接口的视觉模型/默认模型。" />
        </div>
        <Lbl ctx={ctx} k="llm.vision_effort">思考程度</Lbl>
        <EffortField
          value={String(val("llm.vision_effort") ?? "")}
          onChange={(v) => set("llm.vision_effort", v)}
          kind={visProvider?.kind ?? "openai"}
          effectiveModel={effectiveModel}
          models={fetched?.models}
          tip="画面字复核时的思考程度（空 = 不设/使用默认）。一般不需要高思考。"
        />
        <Lbl ctx={ctx} k="ocr.refine_mode">复核方式</Lbl>
        <div className="row"><select className="inp" value={crop ? "crop" : "frame"} onChange={(e) => set("ocr.refine_mode", e.target.value)}>
          <option value="frame">整帧画面（默认）</option><option value="crop">本地截图（省）</option></select>
          <Tip text={"整帧画面：模型看画面字出现时的整帧画面，自己找字、框位置、转写，时间按本地识别的。本地切开的标题会合成一条，本地漏掉的花字、手写字、书法片名会补上；字很小或两边读得不一样的，按原视频分辨率截特写再问一次。\n本地截图：把本地识别时截的每处字的特写连同本地认出的字几处一批发给模型改错。省，但本地切碎的标题合不起来、漏掉的字补不出来，位置用本地的框，本地读错时模型容易顺着错。"} /></div>
        {crop ? <>
          <Lbl ctx={ctx} k="ocr.refine_boxed">特写</Lbl>
          <label className="row"><input type="checkbox" checked={!!val("ocr.refine_boxed")} onChange={(e) => set("ocr.refine_boxed", e.target.checked)} />框出要转写的字
            <Tip text="用截图那一刻的画面重新截每处字的特写，并用红框框出要转写的字（镜头移动时，原来的特写会把片名、别处的字截进来）。" /></label>
          <Lbl ctx={ctx} k="ocr.refine_batch">每批几处</Lbl>
          <div className="row"><select className="inp" value={Number(val("ocr.refine_batch") ?? 4)} onChange={(e) => set("ocr.refine_batch", Number(e.target.value))}>
            <option value={1}>1（每处单独问，不会串图）</option><option value={2}>2</option><option value={4}>4（默认）</option><option value={6}>6（最省）</option></select>
            <Tip text="几处字放在一次请求里问。越多越省；模型偶尔会把同一批另一张图上的字写过来（串图）。" /></div>
          <Lbl ctx={ctx} k={["ocr.refine_context", "ocr.refine_context_width"]}>整帧画面</Lbl>
          <div className="row"><select className="inp" value={val("ocr.refine_context") ? Number(val("ocr.refine_context_width") ?? 640) : 0}
            onChange={(e) => { const w = Number(e.target.value); set("ocr.refine_context", !!w); if (w) set("ocr.refine_context_width", w); }}>
            <option value={0}>不附</option><option value={640}>640 宽（省）</option><option value={1024}>1024 宽</option><option value={1280}>1280 宽（最清楚）</option></select>
            <Tip text="每处字附上它所在时刻的整帧画面，大模型能看到字是印在东西上，还是叠加在画面上的字幕。每张的输入用量：640 约 300 token，1024 约 700，1280 约 1300。" /></div>
        </> : <>
          <Lbl ctx={ctx} k="ocr.refine_frame_width">整帧画面</Lbl>
          <div className="row"><select className="inp" value={Number(val("ocr.refine_frame_width") ?? 1280) >= 1024 ? Number(val("ocr.refine_frame_width") ?? 1280) : 1280}
            onChange={(e) => set("ocr.refine_frame_width", Number(e.target.value))}>
            <option value={1024}>1024 宽（省）</option><option value={1280}>1280 宽（默认）</option></select>
            <Tip text="给模型读的整帧画面缩到多宽。每张的输入用量：1024 约 700 token，1280 约 1300。1024 时细字体、小字容易读错；再宽也不会更准（手写字反而乱读）。" /></div>
        </>}
      </div>
    </div>
  );
}

function TranslateStep({ ctx }: { ctx: StepCtx }) {
  const s = useStore();
  const { val, rawVal, set, doc, modelLists, modelKey, fetchModels } = ctx;
  const providers = (deepGet(doc.effective, "llm.providers") as Provider[] | undefined) ?? [];
  const audioProviders = providers.filter((p) => !p.kind || p.kind === "openai" || p.kind === "agy");
  const rawTrName = String(rawVal("llm.translate") ?? "").trim();
  const effTrName = String(deepGet(doc.effective, "llm.translate") ?? "").trim();
  const activeTrName = rawTrName || effTrName;
  const trProvider = providers.find((p) => p.name === activeTrName);
  const legacyBase = String(deepGet(doc.effective, "llm.base_url") ?? "");
  const trKey = modelKey(trProvider, legacyBase);
  const trFetched = modelLists[trKey];
  const defaultTrModel = trProvider?.model || String(deepGet(doc.effective, "llm.model") ?? "");
  const effectiveTrModel = String(val("llm.translate_model") || defaultTrModel).trim();

  const rawLiName = String(rawVal("llm.listen") ?? "").trim();
  const effLiName = String(deepGet(doc.effective, "llm.listen") ?? "").trim();
  const activeLiName = rawLiName || effLiName || activeTrName;
  const liProvider = providers.find((p) => p.name === activeLiName);
  const liKey = modelKey(liProvider, legacyBase);
  const liFetched = modelLists[liKey];
  const defaultLiModel = liProvider ? (liProvider.listen_model || ((rawLiName || effLiName) ? liProvider.model : "")) : "";
  const effectiveLiModel = String(val("llm.listen_model") || defaultLiModel).trim();

  const mode = String(val("llm.mode") ?? "auto");
  const context = String(val("project.context") ?? "");
  const terms = s.data?.glossary?.terms.length ?? 0;
  return (
    <div className="card">
      <div className="kv">
        <Lbl ctx={ctx} k="llm.mode">方式</Lbl>
        <div className="row"><Seg value={mode} options={Object.entries(LLM_MODE)} onChange={(v) => set("llm.mode", v)} />
          <span className="hint">{mode === "export" ? "把翻译任务写成文件，交给 Claude Code 之类的助手完成后再运行一次" : mode === "off" ? "只做识别和合并，不输出译文" : mode === "auto" ? "配了接口就调接口，否则导出任务文件" : ""}</span></div>
        <Lbl ctx={ctx} k="llm.translate">接口</Lbl>
        <div className="row"><select className="inp" value={rawTrName} onChange={(e) => set("llm.translate", e.target.value || undefined)}>
          <option value="">跟随全局设置{effTrName ? `（${effTrName}）` : ""}</option>{providers.map((p) => <option key={p.name} value={p.name}>{providerOpt(p)}</option>)}</select>
          <span className="hint">支持 OpenAI 兼容接口和本机 CLI（agy / codex）；在「设置」里管理</span></div>
        <Lbl ctx={ctx} k="llm.translate_model">模型</Lbl>
        <div className="row">
          <ModelPicker
            value={String(val("llm.translate_model") ?? "")}
            placeholder={defaultTrModel ? `默认：${defaultTrModel}` : "可留空（用接口默认模型）"}
            allowClear
            models={trFetched?.models}
            loading={trFetched?.loading}
            error={trFetched?.error}
            loaded={trFetched?.loaded}
            onFetch={() => fetchModels(trProvider, legacyBase)}
            onChange={(v) => set("llm.translate_model", v)}
          />
          <Tip text="可在当前作品 / 剧集上单独覆盖翻译模型；留空则使用全局接口默认模型。" />
        </div>
        <Lbl ctx={ctx} k="llm.reasoning_effort">思考程度</Lbl>
        <EffortField
          value={String(val("llm.reasoning_effort") ?? "")}
          onChange={(v) => set("llm.reasoning_effort", v)}
          kind={trProvider?.kind ?? "openai"}
          effectiveModel={effectiveTrModel}
          models={trFetched?.models}
          tip="请求里的 reasoning_effort，只用在翻译正文（普通分段 / 分组翻）；整理、人物表、分组、补翻不用。若所选接口为 agy，设了此项会优先覆盖模型名里的后缀。"
        />
        <Lbl ctx={ctx} k="llm.grouped">分组按顺序翻</Lbl>
        <label className="row"><input type="checkbox" checked={!!val("llm.grouped")} onChange={(e) => set("llm.grouped", e.target.checked)} />按话题几句一组，从头到尾一组一组翻
          <Tip text="代替普通翻译（不是再翻一遍）：先整理一份人物表（同一个人 / 动物的各种叫法对到同一个译名），再按话题把字幕分成几句一组，从头到尾一组一组翻，每次带上前后文、前面已经翻好的译法和这几句的画面。省略主语、改口、调侃这类要看上下文的句子更准，名字和前后译法更统一。比普通翻译慢，请求次数多（每组一次），前后文会重复发送；某一组失败时，那几句之后逐条补翻。" /></label>
        <Lbl ctx={ctx} k="llm.frames">参考画面</Lbl>
        <label className="row"><input type="checkbox" checked={!!val("llm.frames")} onChange={(e) => set("llm.frames", e.target.checked)} />每条附一张画面截图
          <Tip text="翻译时每条字幕附一张它出现时的画面小图，让大模型看到场景再翻（比如「揚げる」是炸还是收网、省略的主语是谁）。输入用量大约多一倍；接口不支持图片时自动改为不带图。" /></label>
        <Lbl ctx={ctx} k="llm.listen_audio">参考语音</Lbl>
        <label className="row"><input type="checkbox" checked={!!val("llm.listen_audio")} onChange={(e) => set("llm.listen_audio", e.target.checked)} />翻译前先听对话原声，标出问句 / 陈述、语气
          <Tip text="翻译前把对话的原声一段段（几十秒）发给能听音频的模型，逐条标出问句还是陈述、说话的语气（生气地骂、开玩笑……），只有语音识别来源的句子还标出听到的说法，翻译时附在每条后面作参考。解决光看文字分不出的：「…あれなの」是在讲解还是在问、「いじめて」是骂还是逗。支持 OpenAI 兼容接口和 Antigravity CLI（agy）。" /></label>
        {!!val("llm.listen_audio") && (<>
          <Lbl ctx={ctx} k="llm.listen">听音接口</Lbl>
          <div className="row">
            <select className="inp" value={rawLiName} onChange={(e) => set("llm.listen", e.target.value || undefined)}>
              <option value="">跟随默认{effLiName ? `（${effLiName}）` : activeTrName ? `（同翻译：${activeTrName}）` : ""}</option>
              {audioProviders.map((p) => <option key={p.name} value={p.name}>{providerOpt(p)}</option>)}
            </select>
            {liProvider?.kind === "codex" && <span className="hint" style={{ color: "var(--warn)" }}>Codex CLI 不支持音频，请在左侧选 OpenAI 接口或 agy</span>}
          </div>
          <Lbl ctx={ctx} k="llm.listen_model">听音模型</Lbl>
          <div className="row">
            <ModelPicker
              value={String(val("llm.listen_model") ?? "")}
              placeholder={defaultLiModel ? `默认：${defaultLiModel}` : "必填：支持音频的模型（或先在设置页填听音模型）"}
              filterModality="audio"
              allowClear
              models={liFetched?.models}
              loading={liFetched?.loading}
              error={liFetched?.error}
              loaded={liFetched?.loaded}
              onFetch={() => fetchModels(liProvider, legacyBase, liProvider?.kind === "codex" ? "Codex CLI 不支持音频" : "")}
              onChange={(v) => set("llm.listen_model", v)}
            />
          </div>
          <Lbl ctx={ctx} k="llm.listen_effort">听音思考</Lbl>
          <EffortField
            value={String(val("llm.listen_effort") ?? "")}
            onChange={(v) => set("llm.listen_effort", v)}
            kind={liProvider?.kind ?? "openai"}
            effectiveModel={effectiveLiModel}
            models={liFetched?.models}
            tip="听音标注阶段的思考程度（空 = 不设/使用模型默认）。通常 low 或 medium 就够用。"
          />
        </>)}
        {ctx.episode && (<>
          <span className="lbl">背景资料</span>
          <div className="col" style={{ gap: 4 }}>
            <div className="muted">节目说明：{context ? `「${context.length > 40 ? context.slice(0, 40) + "…" : context}」` : "没写"}</div>
            <div className="muted">术语表 {terms} 条</div>
            <span className="link" onClick={() => ctx.onTab("notes")}>在「资料」里编辑 →</span>
          </div>
        </>)}
      </div>
    </div>
  );
}

function OrganizeStep({ ctx }: { ctx: StepCtx }) {
  const { val, rawVal, set, doc, modelLists, modelKey, fetchModels } = ctx;
  const providers = (deepGet(doc.effective, "llm.providers") as Provider[] | undefined) ?? [];
  const rawName = String(rawVal("llm.organize") ?? "").trim();
  const effName = String(deepGet(doc.effective, "llm.organize") ?? "").trim();
  const trName = String(val("llm.translate") ?? "").trim();
  const orgName = rawName || effName || trName;
  const orgProvider = providers.find((p) => p.name === orgName);
  const legacyBase = String(deepGet(doc.effective, "llm.base_url") ?? "");
  const mKey = modelKey(orgProvider, legacyBase);
  const fetched = modelLists[mKey];
  const defaultModel = orgProvider?.model || String(deepGet(doc.effective, "llm.model") ?? "");
  const effectiveModel = String(val("llm.organize_model") || defaultModel).trim();
  return (
    <div className="card">
      <div className="hint" style={{ marginBottom: 10 }}>用大模型把合成后的字幕整理成一条条字幕，只整理原文、不翻译：几路来源说的同一句话合成一条（半句在字幕、半句在画面字里的也接上），按上下文和术语表纠正画面字、语音识别的错字，分出台词、旁白、说明，丢掉画面上的杂字和重复的语音。原来分开的几屏照样分开，要不要合成一条、怎么分屏，翻译时按译文再定。</div>
      <div className="kv">
        <Lbl ctx={ctx} k="llm.organize">接口</Lbl>
        <div className="row"><select className="inp" value={rawName} onChange={(e) => set("llm.organize", e.target.value || undefined)}>
          <option value="">跟随默认{effName ? `（${effName}）` : trName ? `（同翻译：${trName}）` : ""}</option>{providers.map((p) => <option key={p.name} value={p.name}>{providerOpt(p)}</option>)}</select>
          <span className="hint">可与翻译拆开，例如整理用便宜快速的模型、翻译用旗舰模型</span></div>
        <Lbl ctx={ctx} k="llm.organize_model">模型</Lbl>
        <div className="row">
          <ModelPicker
            value={String(val("llm.organize_model") ?? "")}
            placeholder={defaultModel ? `默认：${defaultModel}` : "可留空（用接口默认模型）"}
            allowClear
            models={fetched?.models}
            loading={fetched?.loading}
            error={fetched?.error}
            loaded={fetched?.loaded}
            onFetch={() => fetchModels(orgProvider, legacyBase)}
            onChange={(v) => set("llm.organize_model", v)}
          />
          <Tip text="可在当前作品或剧集上覆盖整理阶段用的模型；留空则用该接口的默认模型。" />
        </div>
        <Lbl ctx={ctx} k="llm.organize_effort">思考程度</Lbl>
        <EffortField
          value={String(val("llm.organize_effort") ?? "")}
          onChange={(v) => set("llm.organize_effort", v)}
          kind={orgProvider?.kind ?? "openai"}
          effectiveModel={effectiveModel}
          models={fetched?.models}
          tip="整理阶段（含生成术语表、说话人拆分辅助判断）的思考程度；空 = 不设/使用默认。"
        />
        <Lbl ctx={ctx} k="subs.skip_sound_annotations">声音说明</Lbl>
        <label className="row">
          <input type="checkbox" checked={!!val("subs.skip_sound_annotations")} onChange={(e) => set("subs.skip_sound_annotations", e.target.checked)} />
          跳过声音说明
          <span className="hint">整理时由大模型结合上下文跳过笑声、掌声、环境音、伴奏等标注，保留台词、歌词和说话人标注。</span>
        </label>
      </div>
    </div>
  );
}

function FuseStats() {
  const s = useStore();
  const st = s.data?.fused.stats;
  if (!st) return null;
  const cues = s.data!.cues;
  return (
    <div className="card">
      <h3>结果</h3>
      <div className="stats">
        <div className="stat"><div className="n" style={{ color: "var(--c-sub)" }}>{st.subs}</div><div className="d">字幕条目</div></div>
        <div className="stat"><div className="n" style={{ color: "var(--c-ocr)" }}>{st.ocr_total}</div>
          <div className="d">画面字：台词 {st.telop} · 说明 {st.info} · 重复 {st.ocr_dup} · 丢弃 {st.ocr_logo + st.ocr_junk + st.ocr_scene}</div></div>
        <div className="stat"><div className="n" style={{ color: "var(--c-asr)" }}>{st.asr_total}</div><div className="d">语音：无覆盖 {st.asr_uncovered} · 作为参考 {st.asr_hint}</div></div>
        <div className="stat"><div className="n" style={{ color: "var(--acc)" }}>{cues.length}</div><div className="d">最终字幕 · 待审校 {cues.filter((c) => c.review === "todo").length}</div></div>
      </div>
    </div>
  );
}

/** 这一步上次（或这一次正在跑）的日志 */
function LastRun({ stage, ctx }: { stage: string; ctx: StepCtx }) {
  const s = useStore();
  const [saved, setSaved] = useState<string[] | null>(null);
  const [all, setAll] = useState(false);
  const st = ctx.status(stage as StepId) || "";
  useEffect(() => {
    setSaved(null);
    if (!s.pid || stage === "off") return;
    api.stageLog(s.pid, stage).then((r) => setSaved(r.lines)).catch(() => setSaved([]));
  }, [stage, s.pid, s.version]);
  if (stage === "off") return null;
  const live = s.logs.filter((l) => l.stage === stage).map((l) => l.msg);
  const fresh = st === "pending" || st === "running";
  const lines = live.length ? live : fresh ? [] : saved ?? [];
  const sec = ctx.seconds(stage);
  const job = s.summary?.job;
  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>{live.length || fresh ? "这一次运行" : "上次运行"}</h3>
        {sec ? <span className="dim">{sec}s</span> : null}
        <span className="sp" /><button className="btn sm ghost" onClick={() => setAll(true)}>全部日志</button>
      </div>
      {st === "failed" && job?.error ? <div className="run-err">{job.error}</div>
        : s.summary?.failed?.stage === stage && !s.running && <div className="run-err" title={s.summary.failed.error ?? ""}>上次运行在这一步失败：{briefError(s.summary.failed.error)}</div>}
      <LogView lines={lines} empty={st === "pending" ? "这一轮还没运行到这一步" : st === "running" ? "运行中…" : saved === null ? "加载中…" : "这一步还没有日志（还没运行过，或者直接复用了以前的结果）"} />
      {all && <AllLogs onClose={() => setAll(false)} />}
    </div>
  );
}

export function AllLogs({ onClose }: { onClose: () => void }) {
  const s = useStore();
  // 这次打开以后没运行过（或程序重启过）：按步骤顺序把每一步上次的日志拼起来，不用一步步点开看
  const [saved, setSaved] = useState<string[] | null>(null);
  useEffect(() => {
    if (s.logs.length || !s.pid) return;
    const pid = s.pid;
    const stages = (s.summary?.plan ?? []).map((p) => p.stage);
    Promise.all(stages.map((st) => api.stageLog(pid, st).then((r) => r.lines).catch(() => [] as string[])))
      .then((all) => setSaved(all.flatMap((lines, i) => (lines.length ? [`── ${STAGE_LBL[stages[i]] ?? stages[i]} ──`, ...lines, ""] : []))));
  }, [s.pid, s.logs.length, s.summary?.plan]);
  const failed = s.summary?.failed;
  const head = failed ? [`✕ 上次${failed.stage ? `在「${STAGE_LBL[failed.stage] ?? failed.stage}」` : ""}失败：${failed.error ?? ""}`, ""] : [];
  const lines = s.logs.length ? s.logs.map((l) => (l.stage ? `[${STAGE_LBL[l.stage] ?? l.stage}] ${l.msg}` : l.msg)) : saved ?? [];
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return createPortal(
    <div className="modal" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal-body log-dialog">
        <div className="row" style={{ marginBottom: 10 }}><h3 style={{ margin: 0 }}>全部日志</h3><span className="dim">{s.logs.length ? "这一次运行" : "各步骤上次运行"}</span><span className="sp" /><button className="btn sm" onClick={onClose}>关闭</button></div>
        <LogView lines={lines.length ? [...head, ...lines] : head} empty={saved === null && !s.logs.length ? "加载中…" : "（还没有运行过）"} />
      </div>
    </div>, document.body);
}

function LogView({ lines, empty }: { lines: string[]; empty: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [lines]);
  return <div className="log" ref={ref}>{lines.length ? lines.join("\n") : empty}</div>;
}
