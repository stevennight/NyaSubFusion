// 作品页：一部作品的东西放一起——各集、默认设置（新建这部作品的集时复制一份）、各集共用的节目说明和术语。
import { useEffect, useState } from "react";
import { api } from "../api";
import { TemplateEditor } from "../components/TemplateEditor";
import { useStore } from "../store";
import type { SeriesDoc } from "../types";
import { useOpenVideo } from "./Library";
import { SeriesNotes } from "./Notes";

export type SeriesTab = "eps" | "settings" | "notes";

export function SeriesPage({ name, tab, setTab, onOpened, onRenamed }: {
  name: string; tab: SeriesTab; setTab: (t: SeriesTab) => void; onOpened: () => void; onRenamed: (to: string) => void;
}) {
  const s = useStore();
  const [doc, setDoc] = useState<SeriesDoc | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const ov = useOpenVideo(onOpened);
  const load = () => api.seriesDoc(name).then(setDoc).catch((e) => s.setError(e.message));
  useEffect(() => { setDoc(null); load(); }, [name, tab]);  // eslint-disable-line react-hooks/exhaustive-deps
  const rename = async () => {
    const to = (renaming ?? "").trim();
    setRenaming(null);
    if (!to || to === name) return;
    try { await api.renameSeries(name, to); s.toast(`已改名为「${to}」，各集的所属作品一起改了`); onRenamed(to); if (s.pid) s.reload(); }
    catch (e) { s.setError((e as Error).message); }
  };
  const openEp = (pid: string, video: string) => { if (s.pid === pid) onOpened(); else ov.open(video); };
  return (
    <div className="page"><div className="wide">
      <div className="row" style={{ marginBottom: 12 }}>
        {renaming === null ? <h2 style={{ margin: 0 }}>{name}</h2>
          : <input className="inp title-inp" autoFocus value={renaming} onChange={(e) => setRenaming(e.target.value)} onBlur={rename}
                   onKeyDown={(e) => { if (e.key === "Enter") rename(); if (e.key === "Escape") setRenaming(null); }} />}
        {doc && <span className="chip">{doc.episodes.length} 集</span>}
        {doc && (doc.has_settings ? <span className="chip tpl">有默认设置</span> : <span className="chip">还没有默认设置</span>)}
        <span className="sp" />
        <button className="btn" onClick={() => setRenaming(name)}>重命名</button>
        <button className="btn pri" disabled={ov.busy} onClick={() => ov.pick(name)}>＋ 加一集</button>
      </div>
      <div className="tabs" style={{ marginBottom: 14 }}>
        <button className={tab === "eps" ? "on" : ""} onClick={() => setTab("eps")}>各集{doc ? <small>{doc.episodes.length}</small> : null}</button>
        <button className={tab === "settings" ? "on" : ""} onClick={() => setTab("settings")}>默认设置</button>
        <button className={tab === "notes" ? "on" : ""} onClick={() => setTab("notes")}>节目说明与术语</button>
      </div>
      {tab === "eps" && (!doc ? <div className="empty">加载中…</div> : (
        <div className="card">
          {!doc.has_settings && (
            <div className="banner">这部作品还没有默认设置：新建的集会用预设。可以在「默认设置」里设，或者用某一集的设置当默认设置（下面每集右边）。</div>
          )}
          {doc.episodes.length === 0 ? <div className="dim">还没有集。点右上角“加一集”。</div> : (
            <table className="tb"><thead><tr><th>集</th><th>和默认设置相比</th><th /></tr></thead><tbody>
              {doc.episodes.map((e) => (
                <tr key={e.pid} className={e.exists ? "" : "off"}>
                  <td><b style={{ fontWeight: 500 }}>{e.name}</b>{e.running && <span className="chip acc mini">运行中</span>}{!e.exists && <span className="chip bad mini">文件不存在</span>}
                    <div className="hint" style={{ wordBreak: "break-all" }}>{e.video}</div></td>
                  <td>{!doc.has_settings ? <span className="dim">—</span> : e.diff == null ? <span className="dim">？</span>
                    : e.diff ? <span className="chip tpl click" title="打开这一集，在页头点“和模板不同”逐项看" onClick={() => openEp(e.pid, e.video)}>不同 {e.diff} 项</span>
                    : <span className="dim">相同</span>}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    {!doc.has_settings && e.exists && <button className="btn sm tplb" onClick={async () => {
                      try { await api.overwrite(e.pid, "to_series"); s.toast(`已用「${e.name}」的设置当作品默认设置`); load(); } catch (er) { s.setError((er as Error).message); }
                    }}>用这一集的设置当默认设置</button>}{" "}
                    <button className="btn sm" disabled={!e.exists} onClick={() => openEp(e.pid, e.video)}>打开</button>
                  </td>
                </tr>
              ))}
            </tbody></table>)}
          <div className="hint" style={{ marginTop: 8 }}>“不同”是这一集和作品默认设置有差异的项；打开那一集，在页头点开逐项决定用哪边的。改了默认设置想推给已有的集：在「默认设置」里点“同步给已有的集”。</div>
        </div>
      ))}
      {tab === "settings" && <TemplateEditor key={name} target={{ kind: "series", id: name }} embedded />}
      {tab === "notes" && <SeriesNotes name={name} />}
      {ov.ui}
    </div></div>
  );
}
