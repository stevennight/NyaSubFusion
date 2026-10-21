// 全局状态：当前项目、数据、配置、运行中的实时事件。
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, subscribe } from "./api";
import type { Cfg, LangInfo, LiveEvent, LogLine, Progress, ProjectData, ProjectSummary, StageStatus } from "./types";

interface Store {
  pid: string | null;
  summary: ProjectSummary | null;
  data: ProjectData | null;
  config: { project: Cfg; effective: Cfg } | null;
  langs: LangInfo[];
  logs: LogLine[];
  stageProg: Record<string, Progress>;   // 正在跑的步骤的进度（步骤图上显示）
  live: Record<string, StageStatus>;      // 这一轮里各步骤的状态（还没走到的是 pending）
  liveSec: Record<string, number>;        // 这一轮里已完成步骤的用时
  running: boolean;
  progress: Progress | null;    // 输出视频的进度
  version: number;              // 数据 / 配置变了就加一，预览据此刷新
  error: string | null;
  open: (pid: string | null) => void;
  reload: () => Promise<void>;
  setConfig: (project: Cfg) => Promise<void>;
  run: (body?: { until?: string; redo?: string[]; translate?: string }) => Promise<void>;
  exportVideo: (mode: "mux" | "burn", profiles?: string[]) => Promise<void>;
  cancel: () => Promise<void>;
  bump: () => void;
  setError: (e: string | null) => void;
  toast: (msg: string, action?: ToastAction) => void;   // action：提示里带一个按钮（比如撤销后的“重做”）
  toastMsg: string | null;
  toastAction: ToastAction | null;
}

export interface ToastAction { label: string; run: () => void }

const Ctx = createContext<Store | null>(null);
const LAST = "nyasub.project";

export function StoreProvider({ children }: { children: ReactNode }) {
  const [pid, setPid] = useState<string | null>(() => localStorage.getItem(LAST));
  const [summary, setSummary] = useState<ProjectSummary | null>(null);
  const [data, setData] = useState<ProjectData | null>(null);
  const [config, setCfg] = useState<{ project: Cfg; effective: Cfg } | null>(null);
  const [langs, setLangs] = useState<LangInfo[]>([]);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [stageProg, setStageProg] = useState<Record<string, Progress>>({});
  const [live, setLive] = useState<Record<string, StageStatus>>({});
  const [liveSec, setLiveSec] = useState<Record<string, number>>({});
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [version, setVersion] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [toastMsg, setToast] = useState<string | null>(null);
  const [toastAction, setToastAction] = useState<ToastAction | null>(null);
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
      setVersion((v) => v + 1);
      setError(null);
    } catch (e) {
      setError(String((e as Error).message));
      if (String((e as Error).message).includes("没有这个项目")) open(null);
    }
  }, [pid]);

  const open = useCallback((id: string | null) => {
    if (id) localStorage.setItem(LAST, id);
    else localStorage.removeItem(LAST);
    setPid(id);
    setSummary(null);
    setData(null);
    setCfg(null);
    setLogs([]);
    setStageProg({});
    setLive({});
    setLiveSec({});
  }, []);

  useEffect(() => { api.langs().then(setLangs).catch(() => {}); }, []);
  useEffect(() => { reload(); }, [reload]);

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

  const setConfig = useCallback(async (project: Cfg) => {
    if (!pid) return;
    const r = await api.putConfig(pid, project);
    setCfg(r.config);
    setSummary((s) => (s ? { ...s, plan: r.plan } : s));
    setVersion((v) => v + 1);
  }, [pid]);

  const run = useCallback(async (body: { until?: string; redo?: string[]; translate?: string } = {}) => {
    if (!pid) return;
    setLogs([]);
    setStageProg({});
    setLive({});
    setLiveSec({});
    setRunning(true);
    try {
      await api.run(pid, body);
    } catch (e) {
      setRunning(false);
      setError(String((e as Error).message));
    }
  }, [pid]);

  const exportVideo = useCallback(async (mode: "mux" | "burn", profiles?: string[]) => {
    if (!pid) return;
    setLogs([]);
    setStageProg({});
    setLive({});
    setProgress(null);
    setRunning(true);
    try {
      const r = await api.exportVideo(pid, mode, profiles);
      toast(`开始${mode === "mux" ? "封装" : "压制"}：${r.file}`);
    } catch (e) {
      setRunning(false);
      setError(String((e as Error).message));
    }
  }, [pid, toast]);

  const cancel = useCallback(async () => {
    if (!pid) return;
    try { await api.cancel(pid); toast("正在取消…"); } catch (e) { setError(String((e as Error).message)); }
  }, [pid, toast]);

  const value = useMemo<Store>(() => ({
    pid, summary, data, config, langs, logs, stageProg, live, liveSec, running, progress, version, error,
    open, reload, setConfig, run, exportVideo, cancel, bump, setError, toast, toastMsg, toastAction,
  }), [pid, summary, data, config, langs, logs, stageProg, live, liveSec, running, progress, version, error, open, reload, setConfig, run, exportVideo, cancel, bump, toast, toastMsg, toastAction]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore() {
  const s = useContext(Ctx);
  if (!s) throw new Error("StoreProvider 缺失");
  return s;
}
