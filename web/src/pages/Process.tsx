// 处理：左边是步骤列表（就是原来的步骤图，竖着排），点一步，右边是这一步的设置、状态和上次的日志。改了自动保存。
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import { RegionEditor, type Region } from "../components/RegionEditor";
import { REDO_STAGES } from "../components/RunBar";
import { SeriesPicker } from "../components/SeriesPicker";
import { SaveChip, templateChange, useDoc, useDraft, useUnsaved, type ConfigDoc } from "../doc";
import { showVal, stepOf } from "../settingLabels";
import { useStore } from "../store";
import type { Cfg, Change, Provider, StageStatus } from "../types";
import { deepGet, langName, pct, setIn, STAGE_LBL } from "../util";

const STATUS_LBL: Record<string, string> = { pending: "待执行", done: "可复用", cached: "复用", stale: "要重做", missing: "未运行", running: "运行中",
  failed: "失败", waiting: "等外部翻译", cancelled: "已取消", off: "关", empty: "没有内容" };
const DOT: Record<string, string> = { done: "done", cached: "done", stale: "stale", missing: "stale", pending: "stale", running: "run", failed: "fail",
  waiting: "stale", cancelled: "stale", off: "off", empty: "off" };
const SEP_LBL: Record<string, string> = { auto: "自动", on: "开", off: "关" };
const ASR_API_KINDS = {
  transcribe: { label: "转写接口", model: "gpt-transcribe", desc: "OpenAI 兼容的 /v1/audio/transcriptions（gpt-transcribe、whisper-1 等）；只给文字的接口会切成小段识别" },
  gemini: { label: "多模态大模型", model: "gemini-2.5-flash", desc: "Gemini 等能听音频的大模型（chat/completions + 音频），按句给时间，没有逐字时间" },
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

export function ProcessPage({ onTab }: { onTab: (t: "review" | "output" | "notes") => void }) {
  const s = useStore();
  const doc = useDoc();
  // 草稿就是这一份设置；保存时只把改过的项写回去（别的页面同时改了别的项也不会被冲掉）
  const touched = useRef(new Set<string>());
  const { v: draft, update, state } = useDraft<Cfg>(doc, (d) => structuredClone(d.raw),
    (raw, v) => [...touched.current].reduce((out, p) => setIn(out, p, deepGet(v, p)), raw));
  const [step, setStep] = useState<StepId>(() => (localStorage.getItem("nyasub.step") as StepId) || "basic");
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
  const val = (path: string) => deepGet(draft, path) ?? deepGet(eff, path);
  const set = (path: string, value: unknown) => { touched.current.add(path); update((d) => setIn(d, path, value)); };
  const seconds = (st: string) => (st in s.liveSec ? s.liveSec[st] : ["pending", "running"].includes(status[st]) ? null
    : s.summary?.plan.find((p) => p.stage === st)?.seconds);
  const note = (st: string) => (st === "render" ? s.summary?.render?.note : s.summary?.plan.find((p) => p.stage === st)?.note);
  const vmode = String(val("render.video.mode") ?? "off");
  const stepStatus = (id: StepId): string => (id === "basic" ? "" : id === "video" ? (vmode === "off" ? "off" : status[vmode] ?? "missing") : status[id] ?? "missing");
  const changes = doc.template?.changes ?? [];
  const diffs = (id: StepId) => changes.filter((c) => stepOf(c.keys) === id);
  const ctx: StepCtx = { doc, val, set, episode, onTab, status: stepStatus, note, seconds, diffs };

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
  doc: ConfigDoc; val: (p: string) => unknown; set: (p: string, v: unknown) => void; episode: boolean;
  onTab: (t: "review" | "output" | "notes") => void;
  status: (id: StepId) => string; note: (st: string) => string | undefined; seconds: (st: string) => number | null | undefined;
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
    case "organize": return "合并来源、分类、丢杂字 · 只出原文";
    case "translate": {
      const effort = String(val("llm.reasoning_effort") ?? "");
      const how = isFine(val) ? " · 精翻" : `${val("llm.grouped") ? " · 分组翻" : ""}${effort ? ` · 思考 ${effort}` : ""}`;
      return `${LLM_MODE[String(val("llm.mode") ?? "auto")]}${how}${val("llm.frames") ? " · 附画面" : ""}`;
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
    organize: () => <OrganizeStep />, translate: () => <TranslateStep ctx={ctx} />, annotate: () => <JumpStep ctx={ctx} text="注音是按图层设的：在「输出」里选一个字幕文件，点开图层就能设注音（振假名、拼音……）。读音不对的写进「资料」里的读音词典。" />,
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

function AsrStep({ ctx }: { ctx: StepCtx }) {
  const { val, set, doc } = ctx;
  const [models, setModels] = useState<{ id: string; label: string; desc: string }[]>([]);
  useEffect(() => { api.asrModels().then(setModels).catch(() => {}); }, []);
  const backend = String(val("asr.backend") ?? "local");
  const kind = (String(val("asr.api_kind") ?? "transcribe") in ASR_API_KINDS ? String(val("asr.api_kind") ?? "transcribe") : "transcribe") as keyof typeof ASR_API_KINDS;
  const providers = (deepGet(doc.effective, "llm.providers") as Provider[] | undefined) ?? [];
  const translateName = String(deepGet(doc.effective, "llm.translate") ?? "");
  const setKind = (k: keyof typeof ASR_API_KINDS) => {
    set("asr.api_kind", k);
    const m = String(val("asr.api_model") ?? "");
    if (!m || Object.values(ASR_API_KINDS).some((x) => x.model === m)) set("asr.api_model", ASR_API_KINDS[k].model);
  };
  const cur = models.find((m) => m.id === val("asr.model"));
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
            {providers.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
          </select>
          <Lbl ctx={ctx} k="asr.api_kind">接口类型</Lbl>
          <div className="row"><div className="seg">{(Object.keys(ASR_API_KINDS) as (keyof typeof ASR_API_KINDS)[]).map((k) =>
            <button key={k} className={kind === k ? "on" : ""} onClick={() => setKind(k)}>{ASR_API_KINDS[k].label}</button>)}</div><Tip text={ASR_API_KINDS[kind].desc} /></div>
          <Lbl ctx={ctx} k="asr.api_model">模型</Lbl>
          <div className="row"><input className="inp" value={String(val("asr.api_model") ?? "")} placeholder={ASR_API_KINDS[kind].model} onChange={(e) => set("asr.api_model", e.target.value)} />
            <Tip text="要选给时间戳的模型。OpenRouter 可以填 mistralai/voxtral-mini-transcribe-3-0" /></div>
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
                <option value="0">自动（OpenRouter 3 分钟，其它 10 分钟）</option>
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
  const { val, set } = ctx;
  const on = val("ocr.refine") === "vlm";
  const crop = val("ocr.refine_mode") === "crop";
  return (
    <div className="card">
      <div className="hint" style={{ marginBottom: 10 }}>视觉大模型重新看画面字，纠正错字，并判断是后期叠加的字幕还是实物上的字（招牌、瓶子标签…）。用「设置 › 用途」里的“画面字复核”接口，接口要支持图片。</div>
      <div className="kv" style={on ? undefined : { opacity: 0.45, pointerEvents: "none" }}>
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
  const { val, set, doc } = ctx;
  const providers = (deepGet(doc.effective, "llm.providers") as Provider[] | undefined) ?? [];
  const mode = String(val("llm.mode") ?? "auto");
  const context = String(val("project.context") ?? "");
  const terms = s.data?.glossary?.terms.length ?? 0;
  const fine = isFine(val);
  return (
    <div className="card">
      <div className="kv">
        <Lbl ctx={ctx} k="llm.mode">方式</Lbl>
        <div className="row"><Seg value={mode} options={Object.entries(LLM_MODE)} onChange={(v) => set("llm.mode", v)} />
          <span className="hint">{mode === "export" ? "把翻译任务写成文件，交给 Claude Code 之类的助手完成后再运行一次" : mode === "off" ? "只做识别和合并，不输出译文" : mode === "auto" ? "配了接口就调接口，否则导出任务文件" : ""}</span></div>
        <Lbl ctx={ctx} k="llm.translate">接口</Lbl>
        <div className="row"><select className="inp" value={String(val("llm.translate") ?? "")} onChange={(e) => set("llm.translate", e.target.value)}>
          <option value="">跟随设置</option>{providers.map((p) => <option key={p.name} value={p.name}>{p.name} · {p.model}</option>)}</select>
          <span className="hint">接口在「设置」里管理；「整理」也用这个接口</span></div>
        <Lbl ctx={ctx} k="llm.frames">参考画面</Lbl>
        <label className="row"><input type="checkbox" checked={!!val("llm.frames")} onChange={(e) => set("llm.frames", e.target.checked)} />每条附一张画面截图
          <Tip text="翻译时每条字幕附一张它出现时的画面小图，让大模型看到场景再翻（比如「揚げる」是炸还是收网、省略的主语是谁）。输入用量大约多一倍；接口不支持图片时自动改为不带图。" /></label>
        <Lbl ctx={ctx} k={["llm.grouped", "llm.reasoning_effort"]}>精翻</Lbl>
        <label className="row"><input type="checkbox" checked={fine} onChange={(e) => { set("llm.grouped", e.target.checked); set("llm.reasoning_effort", e.target.checked ? "high" : ""); }} />分组按顺序翻 + 思考程度 high
          <Tip text="快捷开关：勾上 = 下面两项设成“分组按顺序翻”和思考程度 high（测试里效果最好的组合），去掉 = 两项都关。也可以在下面分别设。" /></label>
        <Lbl ctx={ctx} k="llm.grouped">分组按顺序翻</Lbl>
        <label className="row"><input type="checkbox" checked={!!val("llm.grouped")} onChange={(e) => set("llm.grouped", e.target.checked)} />按话题几句一组，从头到尾一组一组翻
          <Tip text="代替普通翻译（不是再翻一遍）：先整理一份人物表（同一个人 / 动物的各种叫法对到同一个译名），再按话题把字幕分成几句一组，从头到尾一组一组翻，每次带上前后文、前面已经翻好的译法和这几句的画面。省略主语、改口、调侃这类要看上下文的句子更准，名字和前后译法更统一。配合较高的思考程度效果才明显。比普通翻译慢，请求次数多（每组一次），前后文会重复发送；某一组失败时，那几句之后逐条补翻。" /></label>
        <Lbl ctx={ctx} k="llm.reasoning_effort">思考程度</Lbl>
        <div className="row"><input className="inp" list="llm-efforts" style={{ maxWidth: 160 }} placeholder="不设（接口默认）" value={String(val("llm.reasoning_effort") ?? "")}
            onChange={(e) => set("llm.reasoning_effort", e.target.value.trim())} />
          <datalist id="llm-efforts">{["low", "medium", "high", "xhigh"].map((x) => <option key={x} value={x} />)}</datalist>
          <Tip text="请求里的 reasoning_effort，只对会思考的模型有用。各模型支持的档不一样（常见 low / medium / high，有的还有 xhigh、minimal），接口查不到列表，按模型说明填；填了接口不认会自动改成不设。越高想得越久、越慢，思考部分按输出计费。只用在翻译那一次（普通翻译的每一段，或分组翻的每一组）；整理、人物表、分组、补翻、分段这些不用。" /></div>
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

/** 精翻 = 分组按顺序翻 + 思考程度 high（快捷开关，看的是这两项） */
function isFine(val: (p: string) => unknown) {
  return !!val("llm.grouped") && String(val("llm.reasoning_effort") ?? "") === "high";
}

function OrganizeStep() {
  return <Plain text="用大模型把合成后的字幕整理成一条条字幕，只整理原文、不翻译：几路来源说的同一句话合成一条（半句在字幕、半句在画面字里的也接上），按上下文和术语表纠正画面字、语音识别的错字，分出台词、旁白、说明，丢掉画面上的杂字和重复的语音。原来分开的几屏照样分开，要不要合成一条、怎么分屏，翻译时按译文再定。接口和方式在「翻译」里设。" />;
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
      {st === "failed" && job?.error && <div className="run-err">{job.error}</div>}
      <LogView lines={lines} empty={st === "pending" ? "这一轮还没运行到这一步" : st === "running" ? "运行中…" : saved === null ? "加载中…" : "这一步还没有日志（还没运行过，或者直接复用了以前的结果）"} />
      {all && <AllLogs onClose={() => setAll(false)} />}
    </div>
  );
}

function AllLogs({ onClose }: { onClose: () => void }) {
  const s = useStore();
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return createPortal(
    <div className="modal" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal-body log-dialog">
        <div className="row" style={{ marginBottom: 10 }}><h3 style={{ margin: 0 }}>全部日志</h3><span className="sp" /><button className="btn sm" onClick={onClose}>关闭</button></div>
        <LogView lines={s.logs.map((l) => (l.stage ? `[${STAGE_LBL[l.stage] ?? l.stage}] ${l.msg}` : l.msg))} empty="（这次打开以后还没有运行过）" />
      </div>
    </div>, document.body);
}

function LogView({ lines, empty }: { lines: string[]; empty: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [lines]);
  return <div className="log" ref={ref}>{lines.length ? lines.join("\n") : empty}</div>;
}
