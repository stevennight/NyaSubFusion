import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import { useStore } from "../store";
import { RegionEditor, type Region } from "../components/RegionEditor";
import type { Cfg, Progress, StageStatus } from "../types";
import { deepGet, pct, setIn, STAGE_LBL } from "../util";

// 步骤图：从左到右大致按实际运行顺序排（节点上的数字就是运行顺序），连线是“用到了谁的结果”。
// 画面字识别要等语音识别：源语言是“自动判断”时，要靠语音识别检测出语言，才知道用哪种文字的识别模型。
const NODES: { id: string; c: number; r: number }[] = [
  { id: "probe", c: 1, r: 2 }, { id: "audio", c: 2, r: 1 }, { id: "subs", c: 2, r: 2 }, { id: "asr", c: 3, r: 1 },
  { id: "ocr", c: 4, r: 3 }, { id: "ocr_refine", c: 5, r: 3 }, { id: "align", c: 5, r: 2 }, { id: "fuse", c: 6, r: 2 },
  { id: "translate", c: 7, r: 2 }, { id: "annotate", c: 8, r: 2 }, { id: "render", c: 9, r: 2 },
];
const ORDER = ["probe", "audio", "subs", "asr", "ocr", "ocr_refine", "align", "fuse", "translate", "annotate", "render", "mux", "burn"];
const EDGES: [string, string, string?][] = [["probe", "audio"], ["probe", "subs"], ["probe", "ocr"], ["audio", "asr"],
  ["asr", "ocr", "源语言"], ["subs", "align"], ["asr", "align"],
  ["ocr", "ocr_refine"], ["ocr_refine", "fuse"], ["align", "fuse"], ["fuse", "translate"], ["translate", "annotate"], ["annotate", "render"]];
const STATUS_LBL: Record<string, string> = { done: "可复用", cached: "复用", stale: "将重做", missing: "未运行", running: "运行中", failed: "失败", waiting: "等待外部翻译", cancelled: "已取消", off: "未启用", empty: "没有内容" };
const ENCODERS: [string, string][] = [["auto", "自动（先试显卡）"], ["h264_nvenc", "H.264 · NVIDIA 显卡"], ["libx264", "H.264 · CPU"],
  ["hevc_nvenc", "H.265 · NVIDIA 显卡"], ["libx265", "H.265 · CPU"]];

export function PipelinePage({ onReview }: { onReview: () => void }) {
  const s = useStore();
  const [draft, setDraft] = useState<Cfg>({});
  const [until, setUntil] = useState("render");
  const [redoAll, setRedoAll] = useState(false);
  const [saving, setSaving] = useState(false);
  // 服务器上的配置变了（保存、运行结束后刷新）：只有没在改的时候才跟着换；正在改、还没保存的不能被冲掉
  // 刚保存完：一定换成服务器上的（服务器会整理一下再写：去掉空值、调整顺序，和草稿逐字比较会一直显示“未保存”）
  const synced = useRef<{ pid: string | null; cfg: string }>({ pid: null, cfg: "" });
  const adopt = useRef(false);
  useEffect(() => {
    if (!s.config) return;
    const fresh = JSON.stringify(s.config.project);
    const other = synced.current.pid !== s.pid;  // 换了项目：一定换
    const take = other || adopt.current;
    adopt.current = false;
    setDraft((d) => (take || JSON.stringify(d) === synced.current.cfg ? s.config!.project : d));
    synced.current = { pid: s.pid, cfg: fresh };
  }, [s.config, s.pid]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(s.config?.project ?? {});

  const status = useMemo(() => {
    const m: Record<string, StageStatus> = {};
    for (const p of s.summary?.plan ?? []) m[p.stage] = p.status;
    m.render = s.summary?.render?.status ?? "missing";
    // 关着的 / 没内容的步骤：运行时它们也会一闪而过（什么都不做），不显示成“运行中”，免得以为又开始了
    const live = Object.fromEntries(Object.entries(s.live).filter(([k, v]) => !((m[k] === "off" || m[k] === "empty") && v !== "failed")));
    return { ...m, ...live };
  }, [s.summary, s.live]);
  const seconds = Object.fromEntries((s.summary?.plan ?? []).map((p) => [p.stage, p.seconds]));
  const stale = (s.summary?.plan ?? []).filter((p) => !["done", "off", "empty"].includes(p.status));
  const notes: Record<string, string | undefined> = Object.fromEntries((s.summary?.plan ?? []).map((p) => [p.stage, p.note]));
  notes.render = s.summary?.render?.note ?? (s.summary?.render?.files.length ? s.summary.render.files.join("、") : undefined);
  const vmode = String(s.config ? deepGet(s.config.effective, "render.video.mode") ?? "off" : "off");
  const [models, setModels] = useState<{ id: string; label: string; desc: string }[]>([]);
  const [logStage, setLogStage] = useState<string | null>(null);   // 点开了哪一步的日志（"all" = 全部）
  useEffect(() => { api.asrModels().then(setModels).catch(() => {}); }, []);

  const val = (path: string) => deepGet(draft, path) ?? deepGet(s.config?.effective, path);
  const set = (path: string, v: unknown) => setDraft((d) => setIn(d, path, v));

  const save = async () => {
    setSaving(true);
    adopt.current = true;
    try { await s.setConfig(draft); s.toast("配置已保存"); } catch (e) { adopt.current = false; s.setError((e as Error).message); } finally { setSaving(false); }
  };

  return (
    <div className="page"><div className="wide">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>流程</h2><span className="dim">改动任何参数，只重做受影响的步骤</span><span className="sp" />
        <select className="inp" style={{ width: "auto" }} value={until} onChange={(e) => setUntil(e.target.value)}>
          {["subs", "asr", "ocr", "fuse", "translate", "annotate", "render"].map((x) => <option key={x} value={x}>做到：{STAGE_LBL[x]}</option>)}
        </select>
        <label className="row dim"><input type="checkbox" checked={redoAll} onChange={(e) => setRedoAll(e.target.checked)} />全部重做</label>
        {s.running && <button className="btn" onClick={s.cancel}>■ 停止</button>}
        <button className="btn pri" disabled={s.running || dirty} title={dirty ? "先保存配置" : ""}
                onClick={() => s.run({ until, redo: redoAll ? ["all"] : [] })}>{s.running ? "运行中…" : "▶ 运行"}</button>
      </div>
      {stale.length > 0 && !s.running && (
        <div className="banner">⚠ 以下 {stale.length} 个步骤需要运行：{stale.map((p) => STAGE_LBL[p.stage]).join(" → ")}</div>
      )}
      <div className="card"><h3>步骤</h3><StageGraph status={status} seconds={seconds} notes={notes} prog={s.stageProg} video={vmode === "off" ? null : vmode}
                  onOpen={setLogStage} /></div>
      <div className="gap" />

      <div className="pl-body">
        <div className="pl-main">
          <div className="row pl-cfghead">
            <h3 style={{ margin: 0 }}>配置</h3>
            <span className="dim" style={{ fontSize: 12 }}>保存在项目的 project.toml；没写的项用全局 / 作品配置</span>
            <span className="sp" />
            {dirty && <span className="chip warn">未保存</span>}
            {s.running && <span className="chip warn" title="运行开始时就读好了配置：现在保存的改动，要等这次停下或跑完、再运行一次才生效">正在运行：改动下次运行才生效</span>}
            <button className="btn" disabled={!dirty} onClick={() => setDraft(s.config?.project ?? {})}>还原</button>
            <button className="btn pri" disabled={!dirty || saving} onClick={save}>保存配置</button>
          </div>

          <div className="grid2" style={{ gridTemplateColumns: "1.25fr 1fr", alignItems: "stretch" }}>
            <div className="card">
              <h3>项目与语言</h3>
              <div className="kv">
                <span className="lbl">源语言</span>
                <select className="inp" style={{ maxWidth: 220 }} value={String(val("project.source"))} onChange={(e) => set("project.source", e.target.value)}>
                  <option value="auto">自动判断</option>{s.langs.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
                </select>
                <span className="lbl">翻译成</span>
                <LangChips value={(val("project.targets") as string[]) ?? []} onChange={(v) => set("project.targets", v)} />
                <span className="lbl">所属作品</span>
                <input className="inp" placeholder="可不填：同一作品的各集共用节目说明和术语表" value={String(val("project.series") ?? "")} onChange={(e) => set("project.series", e.target.value)} />
                <span className="lbl">节目说明</span>
                <textarea className="inp" rows={3} placeholder="会告诉大模型，例如：北海道渔村纪录片，有大量方言采访" value={String(val("project.context") ?? "")} onChange={(e) => set("project.context", e.target.value)} />
              </div>
            </div>
            <div className="card">
              <h3>大模型</h3>
              <div className="kv">
                <span className="lbl">方式</span>
                <div className="seg">{[["auto", "自动"], ["api", "调接口"], ["export", "导出任务"], ["off", "不翻译"]].map(([v, l]) =>
                  <button key={v} className={val("llm.mode") === v ? "on" : ""} onClick={() => set("llm.mode", v)}>{l}</button>)}</div>
                <span className="lbl">接口</span>
                <select className="inp" value={String(val("llm.translate") ?? "")} onChange={(e) => set("llm.translate", e.target.value)}>
                  <option value="">（跟随全局设置）</option>
                  {((deepGet(s.config?.effective, "llm.providers") as { name: string; model: string }[]) ?? []).map((p) =>
                    <option key={p.name} value={p.name}>{p.name} · {p.model}</option>)}
                </select>
              </div>
              <div className="dim" style={{ fontSize: 12, marginTop: 8 }}>
                {val("llm.mode") === "export" ? "导出任务：把翻译任务写成文件，交给 Claude Code 之类的助手完成后再运行一次。"
                  : val("llm.mode") === "off" ? "不翻译：只做识别和融合，不输出译文字幕。"
                  : "接口在「设置」里管理；API Key 存在系统凭据库，不写进项目文件。"}
              </div>
            </div>
          </div>
          <div className="gap" />

          <div className="card">
            <h3>识别来源</h3>
            <div className="src-cols">
              <div className={`src-col ${val("subs.enabled") ? "" : "off"}`}>
                <label className="row src-head"><input type="checkbox" checked={!!val("subs.enabled")} onChange={(e) => set("subs.enabled", e.target.checked)} /><span className="badge b-c">字幕</span>自带字幕</label>
                <input className="inp" placeholder="指定字幕文件（空 = 自动找同名 / 内封字幕）" value={String(val("subs.path") ?? "")} onChange={(e) => set("subs.path", e.target.value)} />
                <div className="dim" style={{ fontSize: 12, marginTop: 6 }}>最可信的来源：有字幕时以它为准，语音和画面字用来补漏、对时间。</div>
              </div>
              <div className={`src-col ${val("asr.enabled") ? "" : "off"}`}>
                <label className="row src-head"><input type="checkbox" checked={!!val("asr.enabled")} onChange={(e) => set("asr.enabled", e.target.checked)} /><span className="badge b-a">语音</span>语音识别</label>
                <div className="kv">
                  <span className="lbl">模型</span>
                  <select className="inp" value={String(val("asr.model"))} onChange={(e) => set("asr.model", e.target.value)}>
                    {(models.length ? models : [{ id: String(val("asr.model")), label: String(val("asr.model")), desc: "" }]).map((m) =>
                      <option key={m.id} value={m.id}>{m.label}</option>)}
                    {models.length > 0 && !models.some((m) => m.id === val("asr.model")) && <option value={String(val("asr.model"))}>{String(val("asr.model"))}（自定义）</option>}
                  </select>
                  <span />
                  <span className="dim" style={{ fontSize: 11.5 }}>{models.find((m) => m.id === val("asr.model"))?.desc ?? ""}</span>
                  <span className="lbl">人声分离</span>
                  <div className="seg" title="先去掉背景音乐、音效再识别：动画、综艺这类配乐很响的节目，漏句和时间提前会少很多，片头片尾曲也能识别">
                    {[["auto", "自动"], ["on", "开"], ["off", "关"]].map(([v, l]) =>
                      <button key={v} className={String(val("asr.separate") ?? "auto") === v ? "on" : ""} onClick={() => set("asr.separate", v)}>{l}</button>)}</div>
                  <span />
                  <span className="dim" style={{ fontSize: 11.5 }}>自动：没有字幕时才做（这时语音是主要来源）</span>
                  <span className="lbl">人声门槛</span>
                  <div className="row"><input type="range" min={0.1} max={0.6} step={0.05} value={Number(val("asr.vad_threshold"))} onChange={(e) => set("asr.vad_threshold", Number(e.target.value))} /><span className="dim">{String(val("asr.vad_threshold"))}</span></div>
                </div>
              </div>
              <div className={`src-col ${val("ocr.enabled") ? "" : "off"}`}>
                <label className="row src-head"><input type="checkbox" checked={!!val("ocr.enabled")} onChange={(e) => set("ocr.enabled", e.target.checked)} /><span className="badge b-o">画面字</span>画面字识别</label>
                <div className="kv">
                  <span className="lbl">扫描帧率</span>
                  <div className="row"><input type="range" min={1} max={6} step={0.5} value={Number(val("ocr.fps"))} onChange={(e) => set("ocr.fps", Number(e.target.value))} /><span className="dim">{String(val("ocr.fps"))} fps</span></div>
                  <span className="lbl">视觉复核</span>
                  <div className="seg">{["none", "vlm"].map((v) => <button key={v} className={val("ocr.refine") === v ? "on" : ""} onClick={() => set("ocr.refine", v)}>{v === "none" ? "不复核" : "视觉大模型"}</button>)}</div>
                </div>
                {val("ocr.enabled") && s.pid && s.summary?.meta?.video ? (
                  <div className="src-region">
                    <RegionEditor pid={s.pid} duration={s.summary.meta.duration} aspect={s.summary.meta.video.aspect} regions={(val("ocr.regions") as Region[]) ?? [{ rect: [0, 0, 1, 1] }]}
                                  boxes={s.data?.ocr ?? []} onChange={(r) => set("ocr.regions", r)} />
                  </div>
                ) : <div className="dim" style={{ fontSize: 12, marginTop: 6 }}>识别画面上的字幕、人名条、地名；开启后可以框选识别区域。</div>}
              </div>
            </div>
          </div>
          <div className="gap" />
          <VideoCard val={val} set={set} dirty={dirty} />
        </div>

        <aside className="pl-side">
          <RunCard onAll={() => setLogStage("all")} onStage={setLogStage} status={status} />
          <div className="card">
            <div className="row" style={{ marginBottom: 8 }}>
              <h3 style={{ margin: 0 }}>融合结果</h3><span className="sp" />
              <button className="btn sm" disabled={!s.data?.cues.length} onClick={onReview}>去审校 →</button>
            </div>
            <Stats />
          </div>
        </aside>
      </div>
      {logStage && <StageLogDialog stage={logStage} status={status[logStage]} seconds={seconds[logStage]} note={notes[logStage]}
                                   onClose={() => setLogStage(null)} onStage={setLogStage} />}
    </div></div>
  );
}

/** 侧栏：现在在跑哪一步、进度、最近几行日志；完整日志点“全部日志”或点步骤图上的某一步看 */
function RunCard({ onAll, onStage, status }: { onAll: () => void; onStage: (s: string) => void; status: Record<string, StageStatus> }) {
  const s = useStore();
  const cur = Object.keys(s.live).find((k) => s.live[k] === "running");
  const p = cur ? s.stageProg[cur] : undefined;
  const job = s.summary?.job;
  const tail = s.logs.slice(-6);
  return (
    <div className="card run-card">
      <div className="row" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>运行</h3>
        {s.running ? <span className="chip acc">● 运行中</span>
          : job?.status === "failed" ? <span className="chip bad">失败</span>
          : job?.status === "cancelled" ? <span className="chip">已取消</span>
          : job?.status === "done" ? <span className="chip ok">完成</span>
          : job?.status === "waiting" ? <span className="chip warn">等外部翻译</span> : <span className="chip">空闲</span>}
        <span className="sp" />
        <button className="btn sm" onClick={onAll}>全部日志</button>
      </div>
      {s.running && cur && (
        <div className="run-now" onClick={() => onStage(cur)} title="点击看这一步的日志">
          <div className="row"><b>{STAGE_LBL[cur] ?? cur}</b>{p?.label && <span className="dim">{p.label}</span>}<span className="sp" />
            {p && p.total > 0 && <span className="dim">{pct(p)}%</span>}</div>
          {p && p.total > 0 && <div className="pbar" style={{ marginTop: 6 }}><div style={{ width: `${pct(p)}%` }} /></div>}
        </div>
      )}
      {!s.running && job?.status === "failed" && job.error && <div className="run-err">{job.error}</div>}
      <div className="run-tail">
        {tail.length ? tail.map((l, i) => (
          <div key={i} className="ln" onClick={() => l.stage && onStage(l.stage)} title={l.stage ? `${STAGE_LBL[l.stage] ?? l.stage}：点击看这一步的日志` : ""}>
            {l.stage && <span className="tag">{STAGE_LBL[l.stage] ?? l.stage}</span>}{l.msg}
          </div>
        )) : <div className="dim">（运行后显示日志；点步骤图上的步骤可以看那一步上次的日志）</div>}
      </div>
      {!s.running && Object.values(status).some((v) => v === "failed") && <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>失败的步骤在步骤图上标红，点开看原因。</div>}
    </div>
  );
}

/** 某一步的日志（这次运行的；没有就读上次存下的 stages/<步骤>.log），或全部日志 */
function StageLogDialog({ stage, status, seconds, note, onClose, onStage }: {
  stage: string; status?: StageStatus; seconds?: number | null; note?: string; onClose: () => void; onStage: (s: string) => void;
}) {
  const s = useStore();
  const [saved, setSaved] = useState<string[] | null>(null);
  const all = stage === "all";
  const live = all ? s.logs : s.logs.filter((l) => l.stage === stage);
  useEffect(() => {
    setSaved(null);
    if (all || !s.pid) return;
    api.stageLog(s.pid, stage).then((r) => setSaved(r.lines)).catch(() => setSaved([]));
  }, [stage, s.pid]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);
  const p = s.stageProg[stage];
  const lines = live.length ? live.map((l) => (all && l.stage ? `[${STAGE_LBL[l.stage] ?? l.stage}] ${l.msg}` : l.msg)) : saved ?? [];
  return createPortal(
    <div className="modal" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal-body log-dialog">
        <div className="row" style={{ marginBottom: 10 }}>
          <h3 style={{ margin: 0 }}>{all ? "全部日志" : `${STAGE_LBL[stage] ?? stage} 的日志`}</h3>
          {!all && status && <span className="chip">{STATUS_LBL[status] ?? status}{seconds ? ` · ${seconds}s` : ""}</span>}
          {!all && note && <span className="dim">{note}</span>}
          {p && p.total > 0 && <span className="dim">{p.label} {pct(p)}%</span>}
          <span className="sp" />
          {!all && <button className="btn sm ghost" onClick={() => onStage("all")}>全部日志</button>}
          <button className="btn sm" onClick={onClose}>关闭</button>
        </div>
        {!all && !live.length && saved && saved.length > 0 && <div className="dim" style={{ fontSize: 12, marginBottom: 6 }}>上次运行这一步时的日志</div>}
        <LogView lines={lines} empty={all ? "（还没有日志）" : saved === null ? "加载中…" : "这一步还没有日志（还没运行过，或者直接复用了以前的结果）"} />
      </div>
    </div>, document.body);
}

function VideoCard({ val, set, dirty }: { val: (p: string) => unknown; set: (p: string, v: unknown) => void; dirty: boolean }) {
  const s = useStore();
  const mode = String(val("render.video.mode") ?? "off");
  const profiles = ((deepGet(s.config?.effective, "render.profiles") as { name: string; format: string; enabled: boolean }[]) ?? []);
  const picked = (val("render.video.profiles") as string[]) ?? [];
  const toggle = (n: string) => set("render.video.profiles", picked.includes(n) ? picked.filter((x) => x !== n) : [...picked, n]);
  const ready = !!s.data?.cues.length && !s.running && !dirty;
  const p = s.progress;
  const videos = s.summary?.videos ?? {};
  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>输出视频</h3>
        <span className="dim" style={{ fontSize: 12 }}>封装：字幕轨 + 用到的字体放进 MKV，不重新编码，几秒完成；压制：字幕画进画面，任何播放器都能看</span>
      </div>
      <div className="grid2">
        <div className="kv">
          <span className="lbl">运行完自动</span>
          <div className="seg">{[["off", "不输出"], ["mux", "封装 MKV"], ["burn", "压制硬字幕"]].map(([v, l]) =>
            <button key={v} className={mode === v ? "on" : ""} onClick={() => set("render.video.mode", v)}>{l}</button>)}</div>
          <span className="lbl">用哪些方案</span>
          <div className="row wrap">
            <span className={`chip ${picked.length ? "" : "acc"}`} style={{ cursor: "pointer" }} onClick={() => set("render.video.profiles", [])}>全部启用的</span>
            {profiles.map((pr) => <span key={pr.name} className={`chip ${picked.includes(pr.name) ? "acc" : ""}`} style={{ cursor: "pointer" }}
                                        onClick={() => toggle(pr.name)}>{pr.name} · {pr.format.toUpperCase()}</span>)}
          </div>
          <span className="lbl">封装</span>
          <div className="row wrap">
            <select className="inp" style={{ width: "auto" }} value={String(val("render.video.fonts"))} onChange={(e) => set("render.video.fonts", e.target.value)}>
              <option value="subset">嵌入字体（只含用到的字）</option><option value="full">嵌入完整字体</option><option value="none">不嵌入字体</option></select>
            <label className="row dim"><input type="checkbox" checked={!!val("render.video.keep_subs")} onChange={(e) => set("render.video.keep_subs", e.target.checked)} />保留原有字幕轨</label>
          </div>
        </div>
        <div className="kv">
          <span className="lbl">压制编码</span>
          <select className="inp" value={String(val("render.video.encoder"))} onChange={(e) => set("render.video.encoder", e.target.value)}>
            {ENCODERS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          <span className="lbl">画质</span>
          <div className="row"><input type="range" min={14} max={32} step={1} value={Number(val("render.video.quality"))} onChange={(e) => set("render.video.quality", Number(e.target.value))} />
            <span className="dim">{String(val("render.video.quality"))}（越小越清晰，常用 18~23）</span></div>
          <span className="lbl">尺寸</span>
          <select className="inp" style={{ width: "auto" }} value={String(val("render.video.height"))} onChange={(e) => set("render.video.height", Number(e.target.value))}>
            {[[0, "原尺寸"], [2160, "2160p"], [1080, "1080p"], [720, "720p"], [480, "480p"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
        </div>
      </div>
      <div className="row wrap" style={{ marginTop: 10 }}>
        <button className="btn" disabled={!ready} title={dirty ? "先保存配置" : ""} onClick={() => s.exportVideo("mux")}>现在封装 MKV</button>
        <button className="btn" disabled={!ready || !s.summary?.meta?.video} title={dirty ? "先保存配置" : ""} onClick={() => s.exportVideo("burn")}>现在压制硬字幕</button>
        {p && s.running && (<>
          <div className="pbar" style={{ flex: 1, minWidth: 160 }}><div style={{ width: `${pct(p)}%` }} /></div>
          <span className="dim">{STAGE_LBL[p.stage] ?? p.stage} {pct(p)}%</span>
          <button className="btn sm" onClick={s.cancel}>取消</button>
        </>)}
        {!s.running && (videos.mux || videos.burn) && <span className="dim">已输出：{[videos.mux, videos.burn].filter(Boolean).join("、")}</span>}
      </div>
    </div>
  );
}

function LangChips({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const s = useStore();
  return (
    <div className="row wrap">
      {s.langs.map((l) => {
        const on = value.includes(l.code);
        return <span key={l.code} className={`chip ${on ? "acc" : ""}`} style={{ cursor: "pointer" }}
                     title={on && value[0] === l.code ? "第一个目标语言：融合和翻译以它为准" : ""}
                     onClick={() => onChange(on ? value.filter((t) => t !== l.code) : [...value, l.code])}>
          {on && value.length > 1 ? `${value.indexOf(l.code) + 1}. ` : ""}{l.name}</span>;
      })}
    </div>
  );
}

function StageGraph({ status, seconds, notes, prog, video, onOpen }: {
  status: Record<string, StageStatus>; seconds: Record<string, number | null | undefined>; notes: Record<string, string | undefined>;
  prog: Record<string, Progress>; video: string | null; onOpen: (stage: string) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [paths, setPaths] = useState<{ d: string; label?: string; x: number; y: number }[]>([]);
  const nodes = video ? [...NODES, { id: video, c: 10, r: 2 }] : NODES;
  const edges: [string, string, string?][] = video ? [...EDGES, ["render", video]] : EDGES;
  useLayoutEffect(() => {
    const draw = () => {
      const el = box.current;
      if (!el) return;
      const R = el.getBoundingClientRect();
      setPaths(edges.map(([a, b, label]) => {
        const A = el.querySelector(`[data-n="${a}"]`)!.getBoundingClientRect();
        const B = el.querySelector(`[data-n="${b}"]`)!.getBoundingClientRect();
        const x1 = A.right - R.left, y1 = A.top + A.height / 2 - R.top, x2 = B.left - R.left - 2, y2 = B.top + B.height / 2 - R.top;
        const mx = (x1 + x2) / 2;
        return { d: `M${x1} ${y1} C${mx} ${y1},${mx} ${y2},${x2} ${y2}`, label, x: mx, y: (y1 + y2) / 2 };
      }));
    };
    draw();
    window.addEventListener("resize", draw);
    return () => window.removeEventListener("resize", draw);
  }, [video]);
  return (
    <div className="dag" ref={box} style={{ gridTemplateColumns: `repeat(${nodes.length > NODES.length ? 10 : 9}, minmax(0, 1fr))` }}>
      <svg><defs><marker id="ar" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L8 4L0 8z" fill="#5a5a6c" /></marker></defs>
        {paths.map((p, i) => <g key={i}>
          <path d={p.d} stroke={p.label ? "#8a7bd0" : "#4a4a5a"} strokeWidth="1.5" strokeDasharray={p.label ? "4 3" : undefined} fill="none" markerEnd="url(#ar)" />
          {p.label && <text x={p.x + 6} y={p.y - 4} fill="#a99be6" fontSize="11">{p.label}</text>}
        </g>)}</svg>
      {nodes.map((n) => {
        const st = status[n.id] ?? "missing";
        return (
          <div key={n.id} data-n={n.id} className={`node ${st} clickable`} style={{ gridColumn: n.c, gridRow: n.r }}
               title={(notes[n.id] ? notes[n.id] + "\n" : "") + "点击看这一步的日志"} onClick={() => onOpen(n.id)}>
            <span className="st" /><b><span className="ord">{ORDER.indexOf(n.id) + 1}</span>{STAGE_LBL[n.id]}</b>
            {st === "running" && prog[n.id] && prog[n.id].total > 0 ? (<>
              <small>{prog[n.id].label || "运行中"} {pct(prog[n.id])}%</small>
              <div className="nbar"><i style={{ width: `${pct(prog[n.id])}%` }} /></div>
            </>) : <small>{(st === "off" || st === "empty") && notes[n.id] ? notes[n.id] : STATUS_LBL[st] ?? st}
              {st !== "off" && st !== "running" && seconds[n.id] ? ` · ${seconds[n.id]}s` : ""}
              {n.id === "render" && st === "done" && notes.render ? ` · ${notes.render.split("、").length} 个文件` : ""}</small>}
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

function LogView({ lines, empty = "（运行后显示日志）" }: { lines: string[]; empty?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [lines]);
  return <div className="log" ref={ref}>{lines.length ? lines.join("\n") : empty}</div>;
}
