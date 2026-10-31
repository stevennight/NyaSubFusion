import { useEffect, useState, type ReactNode } from "react";
import { EpisodeShell, type EpTab } from "./components/EpisodeShell";
import { PageGuard } from "./components/PageGuard";
import { TemplateEditor, type TplTarget } from "./components/TemplateEditor";
import { LibraryPage } from "./pages/Library";
import { QueuePage } from "./pages/Queue";
import { SeriesPage, type SeriesTab } from "./pages/Series";
import { SettingsPage } from "./pages/Settings";
import { useUndoKeys } from "./doc";
import { useStore } from "./store";

// 左栏只放应用级的：库 · 任务 · 设置。打开一集以后，这一集的工作在页内标签里切换（处理 · 审校 · 输出 · 资料）。
type View = "library" | "queue" | "settings" | "ep" | "tpl" | "series";

const NAV: { v: View; label: string; icon: ReactNode }[] = [
  { v: "library", label: "库", icon: <><path d="M4 5h6v14H4zM14 5h6v14h-6z" /><path d="M6.5 8h1M16.5 8h1" /></> },
  { v: "queue", label: "任务", icon: <><path d="M4 6h16M4 12h16M4 18h10" /><path d="M17 16l3 2-3 2z" /></> },
];
const SETTINGS_ICON = <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1" /></>;

// 旧版记下的页面 → 新的位置
const OLD: Record<string, [View, EpTab?]> = {
  projects: ["library"], pipeline: ["ep", "process"], editor: ["ep", "review"], layers: ["ep", "output"], glossary: ["ep", "notes"],
};
function initial(): [View, EpTab] {
  const v = localStorage.getItem("nyasub.view") ?? "library";
  const t = (localStorage.getItem("nyasub.epTab") as EpTab) || "process";
  if (v in OLD) return [OLD[v][0], OLD[v][1] ?? t];
  return [(["library", "queue", "settings", "ep", "series"].includes(v) ? v : "library") as View, t];
}

export default function App() {
  const s = useStore();
  useUndoKeys(s.toast);
  const [view, setView] = useState<View>(() => initial()[0]);
  const [tab, setTab] = useState<EpTab>(() => initial()[1]);
  useEffect(() => { localStorage.setItem("nyasub.view", view); }, [view]);
  useEffect(() => { localStorage.setItem("nyasub.epTab", tab); }, [tab]);
  useEffect(() => { if (!s.pid && view === "ep") setView("library"); }, [s.pid, view]);
  const openEp = (t: EpTab = "process") => { setTab(t); setView("ep"); };
  // 改模板（作品默认设置 / 自己的预设）：从哪儿进来就回哪儿去
  const [tpl, setTpl] = useState<{ target: TplTarget; back: View } | null>(null);
  const openTpl = (target: TplTarget) => { setTpl({ target, back: view }); setView("tpl"); };
  useEffect(() => { if (view === "tpl" && !tpl) setView("library"); }, [view, tpl]);
  // 作品页
  const [seriesName, setSeriesName] = useState(() => localStorage.getItem("nyasub.series") ?? "");
  const [seriesTab, setSeriesTab] = useState<SeriesTab>("eps");
  useEffect(() => { localStorage.setItem("nyasub.series", seriesName); }, [seriesName]);
  useEffect(() => { if (view === "series" && !seriesName) setView("library"); }, [view, seriesName]);
  const [queueAdd, setQueueAdd] = useState(false);   // 从库里点“批量加入…”：进「任务」直接打开加入对话框
  const openSeries = (name: string, t: SeriesTab = "eps") => { setSeriesName(name); setSeriesTab(t); setView("series"); };

  const series = s.config?.effective?.project?.series as string | undefined;
  return (
    <div className="app">
      <header className="top">
        <div className="logo">
          <svg width="26" height="26" viewBox="0 0 32 32"><path d="M5 8 L9 3 L13 9 Q16 8 19 9 L23 3 L27 8 Q29 13 28 19 Q26 28 16 28 Q6 28 4 19 Q3 13 5 8Z" fill="#ff7aa8" /><circle cx="11.5" cy="16" r="2" fill="#2a0d18" /><circle cx="20.5" cy="16" r="2" fill="#2a0d18" /><path d="M14 21 Q16 23 18 21" stroke="#2a0d18" strokeWidth="1.6" fill="none" strokeLinecap="round" /><rect x="7" y="23.5" width="18" height="2.2" rx="1.1" fill="#fff" opacity=".85" /></svg>
          <span>Nya<b>Sub</b>Fusion</span>
        </div>
        <div className="crumb">
          {view === "tpl" && tpl ? (<>
            <a onClick={() => setView(tpl.back)}>设置</a><span>›</span><span>预设</span><span>›</span>
            <strong className="tplc">{tpl.target.id.replace(/^user:/, "")}</strong>
          </>) : view === "series" ? (<>
            <a onClick={() => setView("library")}>库</a><span>›</span><strong>{seriesName}</strong>
            {seriesTab === "settings" && <><span>›</span><strong className="tplc">默认设置</strong></>}
            {s.summary && <a className="back" style={{ marginLeft: 16 }} title="回到正在做的这一集" onClick={() => setView("ep")}>← 回到「{s.summary.name}」</a>}
          </>) : view === "ep" && s.summary ? (<>
            <a onClick={() => setView("library")}>库</a>
            {series && <><span>›</span><a onClick={() => openSeries(series)}>{series}</a></>}
            <span>›</span><strong title={s.summary.video}>{s.summary.name}</strong>
          </>) : s.summary ? (
            <a className="back" title="回到正在做的这一集" onClick={() => setView("ep")}>← 回到「{s.summary.name}」</a>
          ) : null}
        </div>
        <div className="sp" />
        {s.running && <span className="chip acc" style={{ cursor: "pointer" }} onClick={() => setView("ep")}>● 运行中</span>}
      </header>
      <nav className="nav">
        {NAV.map((n) => (
          <button key={n.v} className={view === n.v || (n.v === "library" && ["ep", "series"].includes(view)) ? "on" : ""} onClick={() => setView(n.v)}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">{n.icon}</svg>{n.label}
          </button>
        ))}
        <div className="sp" />
        <button className={view === "settings" ? "on" : ""} onClick={() => setView("settings")}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">{SETTINGS_ICON}</svg>设置
        </button>
      </nav>
      <main className={`main${view === "ep" ? " ep-main" : ""}`}>
        {s.error && <div className="errbar">⚠ {s.error}<span className="sp" /><button className="btn sm ghost" onClick={() => s.setError(null)}>关闭</button></div>}
        <PageGuard key={view === "ep" ? `ep:${s.pid ?? ""}` : view === "tpl" ? `tpl:${tpl?.target.kind}:${tpl?.target.id}` : view === "series" ? `series:${seriesName}` : view}
                   onBack={() => setView("library")}>
          {view === "library" && <LibraryPage onOpened={() => openEp("process")} onSeries={(n) => openSeries(n)} onQueue={() => { setQueueAdd(true); setView("queue"); }} />}
          {view === "series" && seriesName && <SeriesPage name={seriesName} tab={seriesTab} setTab={setSeriesTab} onOpened={() => openEp("process")}
                                                           onRenamed={(to) => setSeriesName(to)} />}
          {view === "ep" && s.pid && <EpisodeShell tab={tab} setTab={setTab} onSeries={(name) => openSeries(name, "settings")} />}
          {view === "settings" && <SettingsPage onEditPreset={(id) => openTpl({ kind: "preset", id })} />}
          {view === "tpl" && tpl && <TemplateEditor target={tpl.target} onDone={() => setView(tpl.back)} />}
          {view === "queue" && <QueuePage onOpenProject={() => openEp("process")} addOpen={queueAdd} onAddClosed={() => setQueueAdd(false)} />}
        </PageGuard>
      </main>
      {s.toastMsg && (
        <div className="toast">{s.toastMsg}
          {s.toastAction && <button className="btn sm" style={{ marginLeft: 12 }} onClick={() => s.toastAction!.run()}>{s.toastAction.label}</button>}
        </div>
      )}
    </div>
  );
}
