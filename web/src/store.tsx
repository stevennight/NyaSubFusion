// 全局状态：当前项目、数据、配置、运行中的实时事件。
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, subscribe } from "./api";
import type { Cfg, LangInfo, LiveEvent, ProjectData, ProjectSummary, StageStatus } from "./types";

interface Store {
  pid: string | null;
  summary: ProjectSummary | null;
  data: ProjectData | null;
  config: { project: Cfg; effective: Cfg } | null;
  langs: LangInfo[];
  logs: string[];
  live: Record<string, StageStatus>;
  running: boolean;
  version: number;              // 数据 / 配置变了就加一，预览据此刷新
  error: string | null;
  open: (pid: string | null) => void;
  reload: () => Promise<void>;
  setConfig: (project: Cfg) => Promise<void>;
  run: (body?: { until?: string; redo?: string[]; translate?: string }) => Promise<void>;
  bump: () => void;
  setError: (e: string | null) => void;
  toast: (msg: string) => void;
  toastMsg: string | null;
}

const Ctx = createContext<Store | null>(null);
const LAST = "nyasub.project";

export function StoreProvider({ children }: { children: ReactNode }) {
  const [pid, setPid] = useState<string | null>(() => localStorage.getItem(LAST));
  const [summary, setSummary] = useState<ProjectSummary | null>(null);
  const [data, setData] = useState<ProjectData | null>(null);
  const [config, setCfg] = useState<{ project: Cfg; effective: Cfg } | null>(null);
  const [langs, setLangs] = useState<LangInfo[]>([]);
  const [logs, setLogs] = useState<string[]>([]);
  const [live, setLive] = useState<Record<string, StageStatus>>({});
  const [running, setRunning] = useState(false);
  const [version, setVersion] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [toastMsg, setToast] = useState<string | null>(null);
  const toastTimer = useRef(0);

  const toast = useCallback((msg: string) => {
    setToast(msg);
    clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 2600);
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
    setLive({});
  }, []);

  useEffect(() => { api.langs().then(setLangs).catch(() => {}); }, []);
  useEffect(() => { reload(); }, [reload]);

  // 实时事件
  useEffect(() => {
    if (!pid) return;
    api.log(pid).then((evs) => setLogs(evs.filter((e) => e.type === "log").map((e) => e.msg ?? ""))).catch(() => {});
    return subscribe(pid, (e: LiveEvent) => {
      if (e.type === "log" && e.msg) setLogs((l) => [...l.slice(-1500), e.msg!]);
      if (e.type === "stage" && e.name) setLive((m) => ({ ...m, [e.name!]: e.status as StageStatus }));
      if (e.type === "hello") setRunning(e.job?.status === "running");
      if (e.type === "job") {
        setRunning(false);
        setLive({});
        reload();
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
    setLive({});
    setRunning(true);
    try {
      await api.run(pid, body);
    } catch (e) {
      setRunning(false);
      setError(String((e as Error).message));
    }
  }, [pid]);

  const value = useMemo<Store>(() => ({
    pid, summary, data, config, langs, logs, live, running, version, error,
    open, reload, setConfig, run, bump, setError, toast, toastMsg,
  }), [pid, summary, data, config, langs, logs, live, running, version, error, open, reload, setConfig, run, bump, toast, toastMsg]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore() {
  const s = useContext(Ctx);
  if (!s) throw new Error("StoreProvider 缺失");
  return s;
}
