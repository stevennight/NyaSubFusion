// 正在编辑的那一份设置：这一集 / 作品默认设置 / 预设。处理页、输出页只认这一份，自动保存。
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { Cfg, Style } from "./types";

export interface ConfigDoc {
  kind: "episode" | "series" | "preset";
  title: string;                       // 集名 / 作品名 / 预设名
  raw: Cfg;                            // 这一份存的设置
  effective: Cfg;                      // 合并后实际用的
  factory: Record<string, Style>;      // 出厂样式（只读）
  save: (raw: Cfg) => Promise<void>;
  onError: (msg: string) => void;
}

const Ctx = createContext<ConfigDoc | null>(null);

export function DocProvider({ doc, children }: { doc: ConfigDoc | null; children: ReactNode }) {
  return <Ctx.Provider value={doc}>{children}</Ctx.Provider>;
}

export function useDoc(): ConfigDoc | null {
  return useContext(Ctx);
}

export type SaveState = "saved" | "pending" | "saving";

/** 一块设置的草稿：改了 delay 毫秒后自动存进这一份设置（write 把草稿写回原始配置）。草稿只在第一次拿到设置时读一次。 */
export function useDraft<T>(doc: ConfigDoc | null, load: (doc: ConfigDoc) => T, write: (raw: Cfg, v: T) => Cfg, delay = 500) {
  const [v, setV] = useState<T | null>(() => (doc ? load(doc) : null));
  const [state, setState] = useState<SaveState>("saved");
  const dirty = useRef(false);
  const docRef = useRef(doc);
  docRef.current = doc;
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

export function SaveChip({ state }: { state: SaveState }) {
  return <span className={`chip ${state === "saved" ? "" : "warn"}`} title="设置改了会自动保存">{state === "saved" ? "已自动保存" : state === "saving" ? "保存中…" : "待保存"}</span>;
}
