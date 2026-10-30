// 正在编辑的那一份设置：这一集 / 作品默认设置 / 预设。处理页、输出页只认这一份，自动保存。
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { Cfg, Change, Style, TemplateInfo } from "./types";

export interface ConfigDoc {
  kind: "episode" | "series" | "preset";
  title: string;                       // 集名 / 作品名 / 预设名
  raw: Cfg;                            // 这一份存的设置
  effective: Cfg;                      // 合并后实际用的
  factory: Record<string, Style>;      // 出厂样式（只读）
  save: (raw: Cfg) => Promise<void>;
  onError: (msg: string) => void;
  // 只有一集有：它的模板（作品默认设置 / 建它时选的预设），和模板不一样的项，恢复成模板 / 存到模板
  template?: TemplateInfo | null;
  applyTemplate?: (keys: string[][], to: "episode" | "template") => Promise<void>;
}

/** 这一项和模板不一样时，模板里的值（一样就是 undefined） */
export function templateChange(doc: ConfigDoc | null, keys: string[]): Change | undefined {
  const k = keys.join("\u0000");
  return doc?.template?.changes?.find((c) => c.keys.join("\u0000") === k || c.keys.slice(0, keys.length).join("\u0000") === k);
}

const Ctx = createContext<ConfigDoc | null>(null);

export function DocProvider({ doc, children }: { doc: ConfigDoc | null; children: ReactNode }) {
  return <Ctx.Provider value={doc}>{children}</Ctx.Provider>;
}

export function useDoc(): ConfigDoc | null {
  return useContext(Ctx);
}

export type SaveState = "saved" | "pending" | "saving";

// 还没存完的草稿：运行条据此等一等（不然会按改之前的设置运行）
const unsaved = new Set<symbol>();
const listeners = new Set<() => void>();
function markUnsaved(id: symbol, on: boolean) {
  if (on === unsaved.has(id)) return;
  if (on) unsaved.add(id); else unsaved.delete(id);
  listeners.forEach((f) => f());
}
export function useUnsaved(): boolean {
  const [n, setN] = useState(unsaved.size);
  useEffect(() => { const f = () => setN(unsaved.size); listeners.add(f); return () => { listeners.delete(f); }; }, []);
  return n > 0;
}

/** 一块设置的草稿：改了 delay 毫秒后自动存进这一份设置（write 把草稿写回原始配置）。草稿只在第一次拿到设置时读一次。 */
export function useDraft<T>(doc: ConfigDoc | null, load: (doc: ConfigDoc) => T, write: (raw: Cfg, v: T) => Cfg, delay = 500) {
  const [v, setV] = useState<T | null>(() => (doc ? load(doc) : null));
  const [state, setState] = useState<SaveState>("saved");
  const dirty = useRef(false);
  const id = useRef(Symbol("draft"));
  useEffect(() => { markUnsaved(id.current, state !== "saved"); }, [state]);
  const docRef = useRef(doc);
  docRef.current = doc;
  const vRef = useRef(v);
  vRef.current = v;
  // 离开页面时还没到保存的时间：马上存，不丢改动
  useEffect(() => () => {
    markUnsaved(id.current, false);
    const d = docRef.current;
    if (dirty.current && d && vRef.current !== null) d.save(write(d.raw, vRef.current)).catch((e) => d.onError((e as Error).message));
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (v === null && doc) setV(load(doc)); }, [doc]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!dirty.current || v === null) return;
    setState("pending");
    const t = setTimeout(async () => {
      const d = docRef.current;
      if (!d) return;
      dirty.current = false;
      setState("saving");
      try {
        await d.save(write(d.raw, v));
        setState(dirty.current ? "pending" : "saved");
      } catch (e) {
        d.onError((e as Error).message);
        setState("pending");
      }
    }, delay);
    return () => clearTimeout(t);
  }, [v]);  // eslint-disable-line react-hooks/exhaustive-deps
  const update = (f: (cur: T) => T) => { dirty.current = true; setV((cur) => (cur === null ? cur : f(cur))); };
  /** 换成服务器上的（比如恢复成模板以后），不算改动 */
  const reset = () => { dirty.current = false; if (docRef.current) setV(load(docRef.current)); setState("saved"); };
  return { v, update, state, reset };
}

/** 从接口读来、改了自动存回去的一份数据（术语表、读音词典这类不在配置里的）。deps 变了重新读。 */
export function useRemote<T>(fetch: () => Promise<T>, save: (v: T) => Promise<unknown>, deps: unknown[], onError: (m: string) => void, delay = 800) {
  const [v, setV] = useState<T | null>(null);
  const [state, setState] = useState<SaveState>("saved");
  const dirty = useRef(false);
  const id = useRef(Symbol("remote"));
  useEffect(() => { markUnsaved(id.current, state !== "saved"); }, [state]);
  const vRef = useRef(v);
  vRef.current = v;
  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => () => {
    markUnsaved(id.current, false);
    if (dirty.current && vRef.current !== null) saveRef.current(vRef.current).catch((e) => onError((e as Error).message));
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const reload = () => fetch().then((x) => { dirty.current = false; setV(x); setState("saved"); }).catch((e) => onError((e as Error).message));
  useEffect(() => { if (!dirty.current) reload(); }, deps);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!dirty.current || v === null) return;
    setState("pending");
    const t = setTimeout(async () => {
      dirty.current = false;
      setState("saving");
      try { await saveRef.current(v); setState(dirty.current ? "pending" : "saved"); } catch (e) { onError((e as Error).message); setState("pending"); }
    }, delay);
    return () => clearTimeout(t);
  }, [v]);  // eslint-disable-line react-hooks/exhaustive-deps
  const update = (f: (cur: T) => T) => { dirty.current = true; setV((cur) => (cur === null ? cur : f(cur))); };
  return { v, update, state, reload };
}

export function SaveChip({ state }: { state: SaveState }) {
  return <span className={`chip ${state === "saved" ? "" : "warn"}`} title="设置改了会自动保存">{state === "saved" ? "已自动保存" : state === "saving" ? "保存中…" : "待保存"}</span>;
}
