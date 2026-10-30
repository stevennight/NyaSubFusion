// 打开的一集：页头（名字、语言、和模板的差别、运行条）+ 四个标签：处理 · 审校 · 输出 · 资料。
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { DocProvider, type ConfigDoc } from "../doc";
import { EditorPage } from "../pages/Editor";
import { NotesPage } from "../pages/Notes";
import { OutputPage } from "../pages/Output";
import { ProcessPage } from "../pages/Process";
import { useStore } from "../store";
import type { TemplateInfo } from "../types";
import { langName } from "../util";
import { PageGuard } from "./PageGuard";
import { RunBar } from "./RunBar";
import { SavePresetDialog, TemplateDialog, templateTitle } from "./TemplateDialogs";

export type EpTab = "process" | "review" | "output" | "notes";

export function EpisodeShell({ tab, setTab, onSeries }: { tab: EpTab; setTab: (t: EpTab) => void; onSeries: (name: string) => void }) {
  const s = useStore();
  const d = s.data;
  const [template, setTemplate] = useState<TemplateInfo | null>(null);
  const [epoch, setEpoch] = useState(0);   // 设置在页面外面被换掉了（恢复成模板、回到上次运行时）：各页重新读
  const [dialog, setDialog] = useState<"template" | "preset" | null>(null);
  useEffect(() => {
    if (!s.pid) return;
    api.template(s.pid).then(setTemplate).catch(() => setTemplate(null));
  }, [s.pid, s.version]);
  const applyTemplate = useCallback(async (keys: string[][], to: "episode" | "template") => {
    if (!s.pid) return;
    try {
      await api.applyTemplate(s.pid, keys, to);
      await s.reload();
      if (to === "episode") setEpoch((n) => n + 1);
    } catch (e) { s.setError((e as Error).message); }
  }, [s.pid, s.reload, s.setError]);
  // 这一集的设置：处理页、输出页编辑的就是这一份
  const doc = useMemo<ConfigDoc | null>(() => (s.config ? {
    kind: "episode", title: s.summary?.name ?? "", raw: s.config.project, effective: s.config.effective,
    factory: s.config.factory?.styles ?? {}, save: (raw) => s.setConfig(raw), onError: s.setError, template, applyTemplate,
  } : null), [s.config, s.summary?.name, s.setConfig, s.setError, template, applyTemplate]);
  const todo = d?.cues.filter((c) => c.review === "todo").length ?? 0;
  const stale = s.summary?.plan.filter((p) => !["done", "off", "empty"].includes(p.status)).length ?? 0;
  const files = ((s.config?.effective?.render?.profiles ?? []) as { enabled?: boolean }[]).filter((p) => p.enabled !== false).length;
  const terms = d?.glossary?.terms.length ?? 0;
  const nDiff = template?.changes?.length ?? 0;
  const TABS: [EpTab, string, string][] = [
    ["process", "处理", s.running ? "运行中" : stale ? `${stale} 步待运行` : ""],
    ["review", "审校", d?.cues.length ? `${d.cues.length} 条` : ""],
    ["output", "输出", files ? `${files} 个文件` : ""],
    ["notes", "资料", terms ? `术语 ${terms}` : ""],
  ];
  return (
    <div className="ep">
      <div className="ephead">
        <h2 title={s.summary?.video}>{s.summary?.name ?? "加载中…"}</h2>
        {d && <span className="chip">{langName(d.src, s.langs)} → {d.targets.map((t) => langName(t, s.langs)).join("、")}</span>}
        {template?.kind && (
          <span className="chip tpl click" title={`这一集的模板：${templateTitle(template)}。点开看差在哪、逐项恢复`} onClick={() => setDialog("template")}>
            {nDiff ? `和模板不同 ${nDiff} 项` : "和模板一样"}</span>
        )}
        {template?.kind === "series" && <span className="link" onClick={() => onSeries(template.name!)}>改作品默认设置</span>}
        <button className="btn sm ghost" title="把这一集的设置存成一个预设，以后新建项目可以选" onClick={() => setDialog("preset")}>存成预设…</button>
        {d && d.cues.length > 0 && <span className={`chip ${todo ? "warn" : "ok"}`}>{todo ? `待审校 ${todo} / ${d.cues.length}` : "审校完成"}</span>}
        <span className="sp" />
        <RunBar />
      </div>
      <div className="tabs eptabs">
        {TABS.map(([t, label, sub]) => (
          <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{label}{sub && <small>{sub}</small>}</button>
        ))}
      </div>
      <div className={`epbody${tab === "review" ? " fill" : ""}`}>
        <DocProvider doc={doc}>
          <PageGuard key={`${tab}:${epoch}`} onBack={() => setTab("process")}>
            {tab === "process" && <ProcessPage onTab={setTab} />}
            {tab === "review" && <EditorPage />}
            {tab === "output" && <OutputPage />}
            {tab === "notes" && <NotesPage />}
          </PageGuard>
        </DocProvider>
      </div>
      {dialog === "template" && doc && template?.kind && (
        <TemplateDialog doc={doc} onClose={() => setDialog(null)} onRestoredLastRun={() => { s.reload().then(() => setEpoch((n) => n + 1)); setDialog(null); }} />
      )}
      {dialog === "preset" && <SavePresetDialog onClose={() => setDialog(null)} />}
    </div>
  );
}
