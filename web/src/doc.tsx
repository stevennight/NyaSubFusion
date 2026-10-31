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

// 撤销 / 重做（Ctrl+Z / Ctrl+Y）：每块草稿记自己改之前的值，order 记按时间先后是哪块草稿改的。
// 一秒内连着改同一块（拖滑块、打字）算一步。撤销后照常自动保存。离开页面就清掉那一页的记录。
const order: symbol[] = [];
const redoOrder: symbol[] = [];
const hist = new Map<symbol, { undo: () => void; redo: () => void }>();
const drop = (xs: symbol[], id: symbol) => { for (let i = xs.length - 1; i >= 0; i--) if (xs[i] === id) xs.splice(i, 1); };

function useHistory<T>(vRef: { current: T | null }, apply: (v: T) => void) {
  const id = useRef(Symbol("hist"));
  const past = useRef<T[]>([]);
  const future = useRef<T[]>([]);
  const last = useRef(0);
  const applyRef = useRef(apply);
  applyRef.current = apply;
  useEffect(() => {
    const me = id.current;
    hist.set(me, {
      undo: () => { const prev = past.current.pop(); if (prev === undefined || vRef.current === null) return; future.current.push(vRef.current); applyRef.current(prev); },
      redo: () => { const next = future.current.pop(); if (next === undefined || vRef.current === null) return; past.current.push(vRef.current); applyRef.current(next); },
    });
    return () => { hist.delete(me); drop(order, me); drop(redoOrder, me); };
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  /** 改之前调用：记下改之前的值 */
  const record = (prev: T) => {
    const now = Date.now();
    if (now - last.current > 1000 || order[order.length - 1] !== id.current) { past.current.push(prev); order.push(id.current); }
    last.current = now;
    future.current = [];
    redoOrder.length = 0;
  };
  /** 换成服务器上的了（比如恢复成模板）：以前的记录不再适用 */
  const clear = () => { past.current = []; future.current = []; drop(order, id.current); drop(redoOrder, id.current); };
  return { record, clear };
}

/** 撤销 / 重做最近一次设置修改；没有可撤销的返回 false */
export function undoSettings(redo = false): boolean {
  const from = redo ? redoOrder : order, to = redo ? order : redoOrder;
  const id = from.pop();
  const h = id && hist.get(id);
  if (!id || !h) return false;
  (redo ? h.redo : h.undo)();
  to.push(id);
  return true;
}

/** Ctrl+Z 撤销、Ctrl+Y / Ctrl+Shift+Z 重做设置修改。光标在文本框里时交给文本框自己撤销打的字。 */
export function useUndoKeys(toast: (m: string, action?: { label: string; run: () => void }) => void) {
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      const k = e.key.toLowerCase();
      const redo = k === "y" || (k === "z" && e.shiftKey);
      if (k !== "z" && k !== "y") return;
      const t = e.target as HTMLElement | null;
      const typing = t && (t.tagName === "TEXTAREA" || t.isContentEditable || (t.tagName === "INPUT" && ["text", "search", "number", ""].includes((t as HTMLInputElement).type)));
      if (typing) return;
      if (!(redo ? redoOrder : order).length) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (undoSettings(redo)) toast(redo ? "已重做" : "已撤销上一步修改", redo ? undefined : { label: "重做", run: () => { undoSettings(true); } });
    };
    window.addEventListener("keydown", key, true);
    return () => window.removeEventListener("keydown", key, true);
  }, [toast]);
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
  const h = useHistory<T>(vRef, (x) => { dirty.current = true; vRef.current = x; setV(x); });
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
  const update = (f: (cur: T) => T) => {
    const cur = vRef.current;
    if (cur === null) return;
    const nv = f(cur);
    h.record(cur);
    dirty.current = true;
    vRef.current = nv;
    setV(nv);
  };
  /** 换成服务器上的（比如恢复成模板以后），不算改动 */
  const reset = () => { dirty.current = false; h.clear(); if (docRef.current) setV(load(docRef.current)); setState("saved"); };
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
  const h = useHistory<T>(vRef, (x) => { dirty.current = true; vRef.current = x; setV(x); });
  useEffect(() => () => {
    markUnsaved(id.current, false);
    if (dirty.current && vRef.current !== null) saveRef.current(vRef.current).catch((e) => onError((e as Error).message));
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const reload = () => fetch().then((x) => { dirty.current = false; h.clear(); setV(x); setState("saved"); }).catch((e) => onError((e as Error).message));
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
  const update = (f: (cur: T) => T) => {
    const cur = vRef.current;
    if (cur === null) return;
    const nv = f(cur);
    h.record(cur);
    dirty.current = true;
    vRef.current = nv;
    setV(nv);
  };
  return { v, update, state, reload };
}

export function SaveChip({ state }: { state: SaveState }) {
  return <span className={`chip ${state === "saved" ? "" : "warn"}`} title="设置改了会自动保存；改错了按 Ctrl+Z 撤销">{state === "saved" ? "已自动保存" : state === "saving" ? "保存中…" : "待保存"}</span>;
}
