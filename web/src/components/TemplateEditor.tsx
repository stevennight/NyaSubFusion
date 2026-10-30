// 改模板：作品默认设置或者自己的预设。用和一集一样的「处理」「输出」页面，整页变紫、没有运行和日志。
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { DocProvider, useUnsaved, type ConfigDoc } from "../doc";
import { OutputPage } from "../pages/Output";
import { ProcessPage } from "../pages/Process";
import { diffKeys, getKeys } from "../settingLabels";
import { useStore } from "../store";
import type { Cfg, Style } from "../types";
import { PageGuard } from "./PageGuard";
import { SyncDialog } from "./TemplateDialogs";

export interface TplTarget { kind: "series" | "preset"; id: string }
interface Loaded { title: string; raw: Cfg; effective: Cfg; factory: Record<string, Style> }

export function TemplateEditor({ target, onDone, embedded = false }: { target: TplTarget; onDone?: () => void; embedded?: boolean }) {
  const s = useStore();
  const [doc, setDoc] = useState<Loaded | null>(null);
  const [tab, setTab] = useState<"process" | "output">("process");
  const [sync, setSync] = useState(false);
  const snapshot = useRef<Cfg | null>(null);   // 打开时的样子：同步时只同步这之后改了的项
  const unsaved = useUnsaved();
  const load = async () => {
    const r = target.kind === "series" ? await api.seriesDoc(target.id) : await api.preset(target.id);
    const d = { title: target.kind === "series" ? target.id : (r as { name: string }).name, raw: r.raw, effective: r.effective, factory: r.factory.styles };
    if (!snapshot.current) snapshot.current = r.effective;
    setDoc(d);
  };
  useEffect(() => { snapshot.current = null; load().catch((e) => s.setError(e.message)); }, [target.kind, target.id]);  // eslint-disable-line react-hooks/exhaustive-deps
  const cdoc = useMemo<ConfigDoc | null>(() => (doc ? {
    kind: target.kind, title: doc.title, raw: doc.raw, effective: doc.effective, factory: doc.factory, onError: s.setError,
    save: async (raw) => {
      const r = target.kind === "series" ? await api.putSeries(target.id, raw) : await api.putPreset(target.id, raw);
      setDoc((d) => d && { ...d, raw: r.raw, effective: r.effective });
    },
  } : null), [doc, target.kind, target.id, s.setError]);
  if (!doc || !cdoc) return <div className="empty">加载中…</div>;
  // 同步：这次打开以后改了哪些项（作品默认设置里只属于一集的项不会有）
  const changed = target.kind === "series" && snapshot.current ? diffKeys(snapshot.current, doc.effective).filter((k) => k[0] !== "version") : [];
  return (
    <div className={`tplmode${embedded ? "" : " page"}`}>
      <div className="tplbanner">
        <b>{target.kind === "series" ? `正在改「${doc.title}」的默认设置` : `正在改预设「${doc.title}」`}</b>
        <span>{target.kind === "series" ? "新建这部作品的集时复制一份；改这里不影响已有的集，除非你选择同步。" : "新建项目时可以选它；改这里不影响已经建好的项目。"}</span>
        <span className="sp" />
        {unsaved ? <span className="dim">保存中…</span> : <span className="dim">已自动保存</span>}
        {target.kind === "series" && <button className="btn sm tplb" disabled={!changed.length || unsaved} title={changed.length ? "" : "这次打开以后还没改过"}
                                             onClick={() => setSync(true)}>同步给已有的集{changed.length ? `（${changed.length} 项）` : ""}…</button>}
        {onDone && <button className="btn sm tplb" onClick={onDone}>完成</button>}
      </div>
      <div className="tabs" style={{ marginBottom: 4 }}>
        <button className={tab === "process" ? "on" : ""} onClick={() => setTab("process")}>处理</button>
        <button className={tab === "output" ? "on" : ""} onClick={() => setTab("output")}>输出</button>
      </div>
      <DocProvider doc={cdoc}>
        <PageGuard key={tab} onBack={() => setTab("process")}>
          {tab === "process" ? <ProcessPage onTab={(t) => { if (t === "output") setTab("output"); }} /> : <OutputPage />}
        </PageGuard>
      </DocProvider>
      {sync && snapshot.current && (
        <SyncDialog series={target.id} keys={changed} before={changed.map((k) => getKeys(snapshot.current, k))} after={changed.map((k) => getKeys(doc.effective, k))}
                    onClose={() => setSync(false)} />
      )}
    </div>
  );
}
