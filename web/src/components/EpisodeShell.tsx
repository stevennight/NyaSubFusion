// 打开的一集：页头（名字、语言、状态）+ 四个标签：处理 · 审校 · 输出 · 资料。
import { EditorPage } from "../pages/Editor";
import { GlossaryPage } from "../pages/Glossary";
import { LayersPage } from "../pages/Layers";
import { PipelinePage } from "../pages/Pipeline";
import { useStore } from "../store";
import { langName } from "../util";
import { PageGuard } from "./PageGuard";

export type EpTab = "process" | "review" | "output" | "notes";

export function EpisodeShell({ tab, setTab }: { tab: EpTab; setTab: (t: EpTab) => void }) {
  const s = useStore();
  const d = s.data;
  const todo = d?.cues.filter((c) => c.review === "todo").length ?? 0;
  const stale = s.summary?.plan.filter((p) => !["done", "off", "empty"].includes(p.status)).length ?? 0;
  const files = ((s.config?.effective?.render?.profiles ?? []) as { enabled?: boolean }[]).filter((p) => p.enabled !== false).length;
  const terms = d?.glossary?.terms.length ?? 0;
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
        {d && d.cues.length > 0 && <span className={`chip ${todo ? "warn" : "ok"}`}>{todo ? `待审校 ${todo} / ${d.cues.length}` : "审校完成"}</span>}
        <span className="sp" />
        {s.summary && (s.running ? <span className="chip acc">● 运行中</span>
          : <span className={`chip ${stale ? "warn" : "ok"}`}>{stale ? `${stale} 步待运行` : "全部完成"}</span>)}
      </div>
      <div className="tabs eptabs">
        {TABS.map(([t, label, sub]) => (
          <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{label}{sub && <small>{sub}</small>}</button>
        ))}
      </div>
      <div className={`epbody${tab === "review" ? " fill" : ""}`}>
        <PageGuard key={tab} onBack={() => setTab("process")}>
          {tab === "process" && <PipelinePage onReview={() => setTab("review")} />}
          {tab === "review" && <EditorPage />}
          {tab === "output" && <LayersPage />}
          {tab === "notes" && <GlossaryPage />}
        </PageGuard>
      </div>
    </div>
  );
}
