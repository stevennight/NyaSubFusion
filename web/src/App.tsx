import { useEffect, useState, type ReactNode } from "react";
import { EditorPage } from "./pages/Editor";
import { LayersPage } from "./pages/Layers";
import { PipelinePage } from "./pages/Pipeline";
import { ProjectsPage } from "./pages/Projects";
import { useStore } from "./store";
import { langName } from "./util";

type View = "projects" | "pipeline" | "editor" | "layers";

const NAV: { v: View; label: string; icon: ReactNode; needProject?: boolean }[] = [
  { v: "projects", label: "项目", icon: <path d="M3 7h6l2 2h10v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /> },
  { v: "pipeline", label: "流程", needProject: true, icon: <><circle cx="5" cy="12" r="2.5" /><circle cx="19" cy="5" r="2.5" /><circle cx="19" cy="19" r="2.5" /><path d="M7.5 12h4M11.5 12l5-6M11.5 12l5 6" /></> },
  { v: "editor", label: "审校", needProject: true, icon: <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M7 12h6M15 12h2M3 20h18" /></> },
  { v: "layers", label: "图层样式", needProject: true, icon: <><path d="M12 3l9 5-9 5-9-5z" /><path d="M3 13l9 5 9-5" /></> },
];

export default function App() {
  const s = useStore();
  const [view, setView] = useState<View>(() => (localStorage.getItem("nyasub.view") as View) || "projects");
  useEffect(() => { localStorage.setItem("nyasub.view", view); }, [view]);
  useEffect(() => { if (!s.pid && view !== "projects") setView("projects"); }, [s.pid, view]);

  const todo = s.data?.cues.filter((c) => c.review === "todo").length ?? 0;
  const stale = s.summary?.plan.filter((p) => p.status !== "done").length ?? 0;
  return (
    <div className="app">
      <header className="top">
        <div className="logo">
          <svg width="26" height="26" viewBox="0 0 32 32"><path d="M5 8 L9 3 L13 9 Q16 8 19 9 L23 3 L27 8 Q29 13 28 19 Q26 28 16 28 Q6 28 4 19 Q3 13 5 8Z" fill="#ff7aa8" /><circle cx="11.5" cy="16" r="2" fill="#2a0d18" /><circle cx="20.5" cy="16" r="2" fill="#2a0d18" /><path d="M14 21 Q16 23 18 21" stroke="#2a0d18" strokeWidth="1.6" fill="none" strokeLinecap="round" /><rect x="7" y="23.5" width="18" height="2.2" rx="1.1" fill="#fff" opacity=".85" /></svg>
          <span>Nya<b>Sub</b>Fusion</span>
        </div>
        {s.summary && (
          <div className="proj">
            <strong title={s.summary.video}>{s.summary.name}</strong>
            {s.data && <span className="chip">{langName(s.data.src, s.langs)} → {s.data.targets.map((t) => langName(t, s.langs)).join("、")}</span>}
          </div>
        )}
        <div className="sp" />
        {s.summary && (s.running
          ? <span className="chip acc">● 运行中</span>
          : <span className={`chip ${stale ? "warn" : "ok"}`}>● {stale ? `${stale} 步待运行` : "全部完成"}</span>)}
        {s.data && <span className={`chip ${todo ? "warn" : "ok"}`}>{todo ? `${todo} 条待审校` : "审校完成"}</span>}
      </header>
      <nav className="nav">
        {NAV.map((n) => (
          <button key={n.v} className={view === n.v ? "on" : ""} disabled={n.needProject && !s.pid} onClick={() => setView(n.v)}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">{n.icon}</svg>{n.label}
          </button>
        ))}
      </nav>
      <main className="main">
        {s.error && <div className="errbar">⚠ {s.error}<span className="sp" /><button className="btn sm ghost" onClick={() => s.setError(null)}>关闭</button></div>}
        {view === "projects" && <ProjectsPage onOpened={() => setView("pipeline")} />}
        {view === "pipeline" && s.pid && <PipelinePage onReview={() => setView("editor")} />}
        {view === "editor" && s.pid && <EditorPage />}
        {view === "layers" && s.pid && <LayersPage />}
      </main>
      {s.toastMsg && <div className="toast">{s.toastMsg}</div>}
    </div>
  );
}
