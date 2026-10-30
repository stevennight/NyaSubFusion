// 内核接口客户端。开发时由 Vite 代理到本地服务，打包后同源访问。
import type { MediaInfo } from "./types";
import type { Cfg, ConfigState, OverwriteDir, SeriesInfo, ComponentStatus, EditResult, Glossary, QueueOptions, QueueState, Reading, Settings, Term, LangInfo, LiveEvent, PlanItem, Preview, ProjectData, ProjectEntry, ProjectSummary } from "./types";

const params = new URLSearchParams(location.search);
// 桌面壳会注入 token；浏览器里调试时可以写在地址栏 ?token=
export const TOKEN: string = (window as unknown as { __NYASUB_TOKEN__?: string }).__NYASUB_TOKEN__ ?? params.get("token") ?? "";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...(TOKEN ? { "x-nyasub-token": TOKEN } : {}), ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* 不是 JSON */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });
const q = (path: string) => (TOKEN ? `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(TOKEN)}` : path);

export const api = {
  projects: () => call<ProjectEntry[]>("/api/projects"),
  forget: (pid: string) => call<ProjectEntry[]>(`/api/projects/${pid}`, { method: "DELETE" }),
  reveal: (pid: string) => call<{ ok: boolean }>(`/api/projects/${pid}/reveal`, { method: "POST" }),
  open: (path: string) => call<ProjectSummary>("/api/projects", json("POST", { path })),
  project: (pid: string) => call<ProjectSummary>(`/api/projects/${pid}`),
  plan: (pid: string) => call<PlanItem[]>(`/api/projects/${pid}/plan`),
  config: (pid: string) => call<ConfigState>(`/api/projects/${pid}/config`),
  // {project}：这一集的设置；{series}：所属作品的默认设置（两份各自独立）
  putConfig: (pid: string, body: { project?: Cfg; series?: Cfg }) =>
    call<{ config: ConfigState; plan: PlanItem[] }>(`/api/projects/${pid}/config`, json("PUT", body)),
  // 覆盖：to_episode 用作品设置覆盖这一集 / to_series 用这一集的设置覆盖作品。先看会变哪些项
  overwritePreview: (pid: string, direction: OverwriteDir) =>
    call<{ target: "project" | "series"; changes: { path: string; before: unknown; after: unknown }[] }>(
      `/api/projects/${pid}/series/overwrite?direction=${direction}`),
  overwrite: (pid: string, direction: OverwriteDir) =>
    call<{ config: ConfigState; plan: PlanItem[] }>(`/api/projects/${pid}/series/overwrite`, json("POST", { direction })),
  run: (pid: string, body: { until?: string; redo?: string[]; translate?: string }) =>
    call<{ started: boolean }>(`/api/projects/${pid}/run`, json("POST", body)),
  log: (pid: string) => call<LiveEvent[]>(`/api/projects/${pid}/log`),
  stageLog: (pid: string, stage: string) => call<{ lines: string[] }>(`/api/projects/${pid}/stages/${stage}/log`),
  data: (pid: string) => call<ProjectData>(`/api/projects/${pid}/data`),
  preview: (pid: string, profile?: string) =>
    call<Preview>(`/api/projects/${pid}/preview${profile ? `?profile=${encodeURIComponent(profile)}` : ""}`),
  editCue: (pid: string, cid: string, body: Cfg) => call<EditResult>(`/api/projects/${pid}/cues/${cid}`, json("PATCH", body)),
  splitCue: (pid: string, cid: string, at: number) => call<EditResult>(`/api/projects/${pid}/cues/${cid}/split`, json("POST", { at })),
  cuePieces: (pid: string, cid: string, body: { action: "split" | "join"; at: number | "all" } | { action: "cut"; t: number }
    | { action: "type"; seg: number; type: string | null } | { action: "show"; seg: number; show: "on" | "off" | null }
    | { action: "swap"; seg: number; with: number }) =>
    call<EditResult>(`/api/projects/${pid}/cues/${cid}/pieces`, json("POST", body)),
  mergeNext: (pid: string, cid: string) => call<EditResult>(`/api/projects/${pid}/cues/${cid}/merge`, { method: "POST" }),
  insertCue: (pid: string, body: { start: number; end: number; orig?: string }) => call<EditResult>(`/api/projects/${pid}/cues`, json("POST", body)),
  // 被丢弃 / 没用上的画面字恢复成一条字幕（带位置；配了大模型会顺手翻译，note 是没翻成的原因）
  restoreOcr: (pid: string, oid: string) => call<EditResult & { note?: string | null }>(`/api/projects/${pid}/ocr/${oid}/restore`, json("POST", {})),
  // 时间轴右键：以一块来源（字幕 / 画面字 / 语音）新建关联的字幕；并入一条字幕
  cueFromSource: (pid: string, sid: string) => call<EditResult & { note?: string | null }>(`/api/projects/${pid}/sources/${sid}/cue`, json("POST", {})),
  attachSource: (pid: string, cid: string, sid: string) => call<EditResult & { note?: string | null }>(`/api/projects/${pid}/cues/${cid}/attach`, json("POST", { source: sid })),
  clearOrphans: (pid: string) => call<{ ok: boolean }>(`/api/projects/${pid}/edits/orphans`, { method: "DELETE" }),
  // 几条一起改审校状态（播放看过的一批自动标成已审），算一步撤销
  setReview: (pid: string, ids: string[], review: "ok" | "todo" | "flag") => call<EditResult>(`/api/projects/${pid}/review`, json("POST", { ids, review })),
  // 统一译法：原文相同 / 相近、译法不一样的字幕，大模型逐组判断要不要统一（改动算一步撤销，改过的标待审校）
  unify: (pid: string) => call<EditResult & { groups: number; same: number; changed: number }>(`/api/projects/${pid}/unify`, { method: "POST" }),
  undoRedo: (pid: string, action: "undo" | "redo") => call<EditResult>(`/api/projects/${pid}/edits/${action}`, { method: "POST" }),
  addReading: (pid: string, body: { lang: string; src: string; reading: string; scope?: string }) =>
    call<{ saved: string }>(`/api/projects/${pid}/readings`, json("POST", body)),
  retranslate: (pid: string, cid: string, body: { instruction?: string; lang?: string }) =>
    call<{ alts: { orig: string; tr: string }[]; lang: string }>(`/api/projects/${pid}/cues/${cid}/retranslate`, json("POST", body)),
  export: (pid: string, profiles?: string[]) => call<{ files: string[]; dir: string }>(`/api/projects/${pid}/export`, json("POST", { profiles })),
  exportVideo: (pid: string, mode: "mux" | "burn", profiles?: string[]) =>
    call<{ started: boolean; file: string }>(`/api/projects/${pid}/export_video`, json("POST", { mode, profiles })),
  cancel: (pid: string) => call<{ ok: boolean }>(`/api/projects/${pid}/cancel`, { method: "POST" }),
  langs: () => call<LangInfo[]>("/api/langs"),
  asrModels: () => call<{ id: string; label: string; desc: string }[]>("/api/asr_models"),
  queue: () => call<QueueState>("/api/queue"),
  enqueue: (paths: string[], options: QueueOptions) => call<QueueState & { added: number; skipped: number }>("/api/queue", json("POST", { paths, options })),
  queueAction: (id: string, action: "remove" | "up" | "down" | "retry" | "cancel") => call<QueueState>(`/api/queue/${id}/${action}`, { method: "POST" }),
  pauseQueue: (paused: boolean) => call<QueueState>("/api/queue/pause", json("POST", { paused })),
  clearQueue: () => call<QueueState>("/api/queue/clear", { method: "POST" }),
  settings: () => call<Settings>("/api/settings"),
  putSettings: (user_config: Cfg) => call<Settings>("/api/settings", json("PUT", { user_config })),
  putKey: (name: string, key: string) => call<{ ok: boolean }>(`/api/settings/keys/${encodeURIComponent(name)}`, json("PUT", { key })),
  deleteKey: (name: string) => call<{ ok: boolean }>(`/api/settings/keys/${encodeURIComponent(name)}`, { method: "DELETE" }),
  testProvider: (body: { name?: string; base_url?: string; model?: string; key?: string }) =>
    call<{ ok: boolean; ms?: number; reply?: string; error?: string }>("/api/settings/test", json("POST", body)),
  presets: () => call<{ id: string; name: string; desc: string }[]>("/api/presets"),
  setup: (pid: string, body: { preset: string; source: string; targets: string[]; series?: string; context?: string }) =>
    call<unknown>(`/api/projects/${pid}/setup`, json("POST", body)),
  series: () => call<SeriesInfo[]>("/api/series"),
  glossary: (pid: string) => call<Glossary>(`/api/projects/${pid}/glossary`),
  putGlossary: (pid: string, terms: Term[]) => call<{ terms: Term[]; plan: PlanItem[] }>(`/api/projects/${pid}/glossary`, json("PUT", { terms })),
  readings: (pid: string) => call<Record<string, Reading[]>>(`/api/projects/${pid}/readings`),
  putReadings: (pid: string, body: Record<string, Reading[]>) => call<Record<string, Reading[]>>(`/api/projects/${pid}/readings`, json("PUT", body)),
  components: () => call<ComponentStatus>("/api/components"),
  installComponent: (name: string, remove = false) => call<unknown>(`/api/components/${name}`, json("POST", { remove })),
  peaks: (pid: string) => call<{ per_second: number; peaks: number[] }>(`/api/projects/${pid}/media/peaks`),
  videoUrl: (pid: string) => q(`/api/projects/${pid}/media/video`),
  mediaSource: (pid: string) => call<MediaInfo>(`/api/projects/${pid}/media/source`),
  hlsUrl: (pid: string) => q(`/api/projects/${pid}/media/hls/index.m3u8`),
  cropUrl: (pid: string, crop: string) => q(`/api/projects/${pid}/${crop}`),
  frameUrl: (pid: string, t: number, h = 360) => q(`/api/projects/${pid}/media/frame?t=${t}&h=${h}`),
};

/** 订阅项目事件（步骤进度、日志），断线自动重连。返回取消函数。 */
export function subscribe(pid: string, onEvent: (e: LiveEvent) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let timer = 0;
  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(q(`${proto}://${location.host}/ws/projects/${pid}`));
    ws.onmessage = (m) => onEvent(JSON.parse(m.data));
    ws.onclose = () => { if (!closed) timer = window.setTimeout(open, 1500); };
  };
  open();
  return () => { closed = true; clearTimeout(timer); ws?.close(); };
}
