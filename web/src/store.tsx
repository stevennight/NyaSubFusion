// 全局状态：当前项目、数据、配置、运行中的实时事件。
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, subscribe } from "./api";
import type { Cfg, ConfigState, CuePatch, LangInfo, LiveEvent, LogLine, Progress, ProjectData, ProjectSummary, Queued, StageStatus } from "./types";

interface Store {
  pid: string | null;
  summary: ProjectSummary | null;
  data: ProjectData | null;
  config: ConfigState | null;
  langs: LangInfo[];
  logs: LogLine[];
  stageProg: Record<string, Progress>;   // 正在跑的步骤的进度（步骤图上显示）
  live: Record<string, StageStatus>;      // 这一轮里各步骤的状态（还没走到的是 pending）
  liveSec: Record<string, number>;        // 这一轮里已完成步骤的用时
  running: boolean;
  queued: Queued | null;        // 还在任务队列里排队（一次只做一件）：排在第几、是什么任务
  progress: Progress | null;    // 输出视频的进度
  version: number;              // 数据 / 配置变了就加一，预览据此刷新
  error: string | null;
  open: (pid: string | null) => void;
  reload: () => Promise<void>;
  /** 改字幕之后：只换掉变了的字幕和注音（不重读整集几 MB 的数据）；来源变了、或者没有 patch 就整个重读 */
  applyEdit: (r: { patch?: CuePatch } | null | undefined) => Promise<void>;
  setConfig: (project: Cfg) => Promise<void>;
  run: (body?: { until?: string; redo?: string[]; translate?: string }) => Promise<void>;
  exportVideo: (mode: "mux" | "burn" | "broadcast", profiles?: string[]) => Promise<void>;   // broadcast：只输出广播字幕原样
  cancel: () => Promise<void>;
  bump: () => void;
  setError: (e: string | null) => void;
  toast: (msg: string, action?: ToastAction) => void;   // action：提示里带一个按钮（比如撤销后的“重做”）
  toastMsg: string | null;
  toastAction: ToastAction | null;
  runOpts: RunOpts;             // 下次运行的选项（运行条和处理页的“下次重做这一步”共用）
  setRunOpts: (f: (o: RunOpts) => RunOpts) => void;
}

export interface RunOpts {
  until: string;          // 运行到哪一步
  redo: string[];         // 一定要重做的步骤（"all" = 全部）
  noCache: boolean;       // 重做的步骤不用缓存（大模型、云端语音识别重新请求）
  clearEdits: boolean;    // 清掉审校时的手动修改
}
export const RUN_DEFAULT: RunOpts = { until: "render", redo: [], noCache: false, clearEdits: false };

export interface ToastAction { label: string; run: () => void }

const Ctx = createContext<Store | null>(null);
const LAST = "nyasub.project";

export function StoreProvider({ children }: { children: ReactNode }) {
  const [pid, setPid] = useState<string | null>(() => localStorage.getItem(LAST));
  const [summary, setSummary] = useState<ProjectSummary | null>(null);
  const [data, setData] = useState<ProjectData | null>(null);
  const [config, setCfg] = useState<ConfigState | null>(null);
  const [langs, setLangs] = useState<LangInfo[]>([]);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [stageProg, setStageProg] = useState<Record<string, Progress>>({});
  const [live, setLive] = useState<Record<string, StageStatus>>({});
  const [liveSec, setLiveSec] = useState<Record<string, number>>({});
  const [running, setRunning] = useState(false);
  const [queued, setQueued] = useState<Queued | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [version, setVersion] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [toastMsg, setToast] = useState<string | null>(null);
  const [toastAction, setToastAction] = useState<ToastAction | null>(null);
  const [runOpts, setRunOptsRaw] = useState<RunOpts>(RUN_DEFAULT);
  const setRunOpts = useCallback((f: (o: RunOpts) => RunOpts) => setRunOptsRaw(f), []);
  const toastTimer = useRef(0);

  const toast = useCallback((msg: string, action?: ToastAction) => {
    setToast(msg);
    setToastAction(action ?? null);
    clearTimeout(toastTimer.current);
    // 带按钮的多留一会儿，来得及点
    toastTimer.current = window.setTimeout(() => { setToast(null); setToastAction(null); }, action ? 6000 : 2600);
  }, []);
  const bump = useCallback(() => setVersion((v) => v + 1), []);

  const reload = useCallback(async () => {
    if (!pid) return;
    try {
      const [s, d, c] = await Promise.all([api.project(pid), api.data(pid), api.config(pid)]);
      setSummary(s);
      setData(d);
      setCfg(c);
      setRunning(s.job?.status === "running");
      setQueued(s.queued ?? null);
      setVersion((v) => v + 1);
      setError(null);
    } catch (e) {
      setError(String((e as Error).message));
      if (String((e as Error).message).includes("没有这个项目")) open(null);
    }
  }, [pid]);

  const dataRef = useRef(data);
  dataRef.current = data;
  const applyEdit = useCallback(async (r: { patch?: CuePatch } | null | undefined) => {
    const p = r?.patch, d = dataRef.current;
    if (!pid || !p || !d || p.sources_changed) return reload();
    const byId = new Map(d.cues.map((c) => [c.id, c]));
    for (const c of p.cues) byId.set(c.id, c);
    if (p.order.some((id) => !byId.has(id))) return reload();   // 对不上（界面上的数据比较旧）：整个重读
    const tokens: ProjectData["tokens"] = {};
    for (const id of p.order) {
      const t = id in p.tokens ? p.tokens[id] : d.tokens[id];
      if (t) tokens[id] = t;
    }
    setData({ ...d, cues: p.order.map((id) => byId.get(id)!), tokens, hardsub: p.hardsub, history: p.history, orphans: p.orphans });
    setVersion((v) => v + 1);
    api.project(pid).then(setSummary).catch(() => {});   // 运行条上“下次运行会做…”：数据小，不用等
  }, [pid, reload]);

  const pidRef = useRef(pid);
  pidRef.current = pid;
  const [tick, setTick] = useState(0);
  const open = useCallback((id: string | null) => {
    if (id && id === pidRef.current) { setTick((n) => n + 1); return; }  // 已经打开的这一集：重新读一遍就行（清掉的话 pid 没变，不会再读）
    if (id) localStorage.setItem(LAST, id);
    else localStorage.removeItem(LAST);
    setPid(id);
    setSummary(null);
    setData(null);
    setCfg(null);
    setQueued(null);
    setLogs([]);
    setStageProg({});
    setLive({});
    setLiveSec({});
    setRunOptsRaw(RUN_DEFAULT);
  }, []);

  useEffect(() => { api.langs().then(setLangs).catch(() => {}); }, []);
  useEffect(() => { reload(); }, [reload, tick]);
  // 还在后台检查有没有广播字幕：过一会儿再问一次
  useEffect(() => {
    if (!pid || summary?.broadcast_check !== "checking") return;
    const t = window.setTimeout(() => api.project(pid).then((s) => { if (pidRef.current === pid) setSummary(s); }).catch(() => {}), 2000);
    return () => clearTimeout(t);
  }, [pid, summary]);

  // 实时事件
  useEffect(() => {
    if (!pid) return;
    api.log(pid).then((evs) => {
      setLogs(evs.filter((e) => e.type === "log").map((e) => ({ msg: e.msg ?? "", stage: e.stage })));
      // 打开页面时正在运行：按这一轮的事件还原各步骤的状态（哪些已完成、哪些还待执行）
      if (evs.some((e) => e.type === "job")) return;
      const m: Record<string, StageStatus> = {}, sec: Record<string, number> = {};
      for (const e of evs) {
        if (e.type === "round") for (const n of e.stages ?? []) m[n] = "pending";
        if (e.type === "stage" && e.name) {
          m[e.name] = e.status as StageStatus;
          if (e.seconds != null) sec[e.name] = e.seconds;
        }
      }
      setLive(m);
      setLiveSec(sec);
    }).catch(() => {});
    return subscribe(pid, (e: LiveEvent) => {
      if (e.type === "round") {  // 新一轮开始：这一轮会走到的步骤都先是“待执行”，不再显示上一轮的用时和日志
        setLive(Object.fromEntries((e.stages ?? []).map((n) => [n, "pending" as StageStatus])));
        setLiveSec({});
        setStageProg({});
      }
      if (e.type === "stage" && e.name && e.seconds != null) setLiveSec((m) => ({ ...m, [e.name!]: e.seconds! }));
      if (e.type === "log" && e.msg) setLogs((l) => [...l.slice(-3000), { msg: e.msg!, stage: e.stage }]);
      if (e.type === "stage" && e.name) setLive((m) => ({ ...m, [e.name!]: e.status as StageStatus }));
      if (e.type === "stage" && e.status === "running") setRunning(true);  // 别处启动的运行（队列、接口）也显示成运行中
      if (e.type === "progress") {
        const p = { stage: e.stage ?? "", done: e.done ?? 0, total: e.total ?? 0, label: e.label };
        setProgress(p);
        if (p.stage) setStageProg((m) => ({ ...m, [p.stage]: p }));
      }
      if (e.type === "stage" && e.name && e.status !== "running") {
        const name = e.name;
        setStageProg((m) => { const n = { ...m }; delete n[name]; return n; });
      }
      if (e.type === "hello") setRunning(e.job?.status === "running");
      if (e.type === "queued") setQueued(e.queued ?? null);
      if (e.type === "job") {
        setRunning(false);
        setProgress(null);
        setStageProg({});
        // 新的步骤状态和用时拿到以后再清掉这一轮的，免得中间闪一下上一轮的用时
        reload().finally(() => { setLive({}); setLiveSec({}); });
        if (e.status === "failed") setError(e.error ?? "运行失败");
      }
    });
  }, [pid, reload]);

  const saveConfig = useCallback(async (body: { project?: Cfg; series?: Cfg }) => {
    if (!pid) return;
    const r = await api.putConfig(pid, body);
    setCfg(r.config);
    setSummary((s) => (s ? { ...s, plan: r.plan } : s));
    setVersion((v) => v + 1);
  }, [pid]);
  const setConfig = useCallback((project: Cfg) => saveConfig({ project }), [saveConfig]);

  const run = useCallback(async (body: { until?: string; redo?: string[]; translate?: string } = {}) => {
    if (!pid) return;
    setLogs([]);
    setStageProg({});
    setLive({});
    setLiveSec({});
    try {  // 排进任务队列：轮到了才开始（开始时会收到步骤事件，显示成运行中）
      setQueued((await api.run(pid, body)).queued);
    } catch (e) {
      setError(String((e as Error).message));
    }
  }, [pid]);

  const exportVideo = useCallback(async (mode: "mux" | "burn" | "broadcast", profiles?: string[]) => {
    if (!pid) return;
    setLogs([]);
    setStageProg({});
    setLive({});
    setProgress(null);
    try {  // 排进任务队列：轮到了才开始
      const r = mode === "broadcast" ? await api.exportBroadcast(pid) : await api.exportVideo(pid, mode, profiles);
      setQueued(r.queued);
      const what = { mux: "封装", burn: "压制", broadcast: "导出广播字幕" }[mode];
      toast(r.queued?.ahead ? `已排进任务队列（前面还有 ${r.queued.ahead} 项），轮到了就${what}：${r.file}` : `开始${what}：${r.file}`);
    } catch (e) {
      setError(String((e as Error).message));
    }
  }, [pid, toast]);

  const cancel = useCallback(async () => {
    if (!pid) return;
    const waiting = !!queued && !running;   // 还在排队：从任务队列里拿掉
    try { await api.cancel(pid); toast(waiting ? "已取消排队" : "正在取消…"); } catch (e) { setError(String((e as Error).message)); }
  }, [pid, toast, queued, running]);

  const value = useMemo<Store>(() => ({
    pid, summary, data, config, langs, logs, stageProg, live, liveSec, running, queued, progress, version, error,
    open, reload, applyEdit, setConfig, run, exportVideo, cancel, bump, setError, toast, toastMsg, toastAction, runOpts, setRunOpts,
  }), [pid, summary, data, config, langs, logs, stageProg, live, liveSec, running, queued, progress, version, error, open, reload, applyEdit, setConfig, run, exportVideo, cancel, bump, toast, toastMsg, toastAction, runOpts, setRunOpts]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore() {
  const s = useContext(Ctx);
  if (!s) throw new Error("StoreProvider 缺失");
  return s;
}
