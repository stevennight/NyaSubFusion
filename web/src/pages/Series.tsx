// 作品页：一部作品的东西放一起——各集、默认设置（新建这部作品的集时复制一份）、各集共用的节目说明和术语。
import { useEffect, useState } from "react";
import { api } from "../api";
import { TemplateEditor } from "../components/TemplateEditor";
import { useStore } from "../store";
import type { SeriesDoc } from "../types";
import { useOpenVideo } from "./Library";
import { SeriesNotes } from "./Notes";
import { briefError, STAGE_LBL } from "../util";

export type SeriesTab = "eps" | "settings" | "notes" | "songs";

interface SongItem {
  id: string; title: string; artist?: string; role?: string; source?: string;
  lines: { orig: string; tr?: Record<string, string> }[];
}

function SeriesSongsPanel({ name }: { name: string }) {
  const s = useStore();
  const [songs, setSongs] = useState<SongItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [title, setTitle] = useState("");
  const [artist, setArtist] = useState("");
  const [role, setRole] = useState("OP");
  const [lyricsText, setLyricsText] = useState("");

  const load = () => {
    setLoading(true);
    api.seriesSongs(name).then((r) => setSongs(r.songs ?? [])).catch((e) => s.setError(e.message)).finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, [name]); // eslint-disable-line react-hooks/exhaustive-deps

  const addSong = async () => {
    const t = title.trim();
    const lines = lyricsText.split(/\r?\n/).map((ln) => ln.trim()).filter(Boolean).map((ln) => {
      const parts = ln.split("||");
      const orig = parts[0].trim();
      const tr = parts[1]?.trim();
      const trMap: Record<string, string> = tr ? { "zh-Hans": tr } : {};
      return { orig, tr: trMap };
    });
    if (!t || !lines.length) return;
    const id = `${t}-${artist}`.toLowerCase().replace(/[^a-z0-9\u3040-\u30ff\u4e00-\u9fff]+/g, "-").replace(/^-|-$/g, "") || `song-${Date.now()}`;
    const next = [...songs.filter((x) => x.id !== id), { id, title: t, artist: artist.trim(), role, source: "manual", lines }];
    try {
      await api.putSeriesSongs(name, next);
      setSongs(next);
      setTitle(""); setArtist(""); setLyricsText("");
      s.toast(`已加入歌曲《${t}》（${lines.length} 句），后续各集将自动对齐复用`);
    } catch (e) { s.setError((e as Error).message); }
  };

  const removeSong = async (id: string) => {
    const next = songs.filter((x) => x.id !== id);
    try {
      await api.putSeriesSongs(name, next);
      setSongs(next);
      s.toast("已移除歌曲");
    } catch (e) { s.setError((e as Error).message); }
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="card">
        <div className="row" style={{ marginBottom: 8 }}>
          <b>作品歌曲库（跨集复用 OP / ED / 插曲）</b>
          <span className="hint">按实际唱到的歌词片段自动模糊对齐，不绑死集数</span>
        </div>
        {loading ? <div className="dim">加载中…</div> : songs.length === 0 ? (
          <div className="dim">还没有收录歌曲。运行翻译时会自动辨曲入库，也可以在下方手工粘贴歌词。</div>
        ) : (
          <table className="tb">
            <thead><tr><th>歌名</th><th>歌手</th><th>类型</th><th>句数</th><th /></tr></thead>
            <tbody>
              {songs.map((sg) => (
                <tr key={sg.id}>
                  <td><b>{sg.title}</b></td>
                  <td>{sg.artist || "—"}</td>
                  <td><span className="chip mini">{sg.role || "song"}</span></td>
                  <td>{sg.lines?.length ?? 0} 句</td>
                  <td style={{ textAlign: "right" }}><button className="btn sm" onClick={() => removeSong(sg.id)}>删除</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="card">
        <div style={{ fontWeight: 500, marginBottom: 8 }}>手工粘贴歌词入库（查不到冷门歌时兜底）</div>
        <div className="row wrap" style={{ gap: 8, marginBottom: 8 }}>
          <input className="inp" style={{ flex: 1, minWidth: 160 }} placeholder="歌名（如 God FATE）" value={title} onChange={(e) => setTitle(e.target.value)} />
          <input className="inp" style={{ flex: 1, minWidth: 140 }} placeholder="歌手（可选）" value={artist} onChange={(e) => setArtist(e.target.value)} />
          <select className="inp" style={{ width: 110 }} value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="OP">OP</option><option value="ED">ED</option><option value="insert">插曲/童谣</option>
          </select>
        </div>
        <textarea className="inp" rows={6} style={{ width: "100%", fontFamily: "inherit" }}
                  placeholder={"每行一句原文；如需同时锁定中文译文，写成：原文 || 中文译文"}
                  value={lyricsText} onChange={(e) => setLyricsText(e.target.value)} />
        <div className="row" style={{ marginTop: 8 }}>
          <span className="hint">支持粘贴完整版歌词（1 番 + 2 番），各集会自动对齐实际唱到的段落</span>
          <span className="sp" />
          <button className="btn pri" disabled={!title.trim() || !lyricsText.trim()} onClick={addSong}>保存到作品歌曲库</button>
        </div>
      </div>
    </div>
  );
}

function SeriesResearchBar({ name, onApplied }: { name: string; onApplied: () => void }) {
  const s = useStore();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState(name);
  const [pasted, setPasted] = useState("");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<{ context: string; terms: { src: string; reading?: string; tr?: string; type?: string; note?: string; aliases?: string[]; relations?: string }[]; sources: string[] } | null>(null);

  const run = async (usePasted: boolean) => {
    setBusy(true);
    try {
      const res = await api.researchSeries(name, usePasted ? { query, pasted_text: pasted } : { query });
      setDraft(res);
      if (!res.context && !res.terms?.length) s.toast("未检索到资料，可尝试粘贴网页文本由 AI 整理");
    } catch (e) { s.setError((e as Error).message); }
    finally { setBusy(false); }
  };

  const apply = async () => {
    if (!draft) return;
    try {
      const cur = await api.seriesGlossary(name);
      const existing = cur.terms ?? [];
      const seen = new Set(existing.map((t) => t.src));
      const merged = [...existing, ...draft.terms.filter((t) => t.src && !seen.has(t.src)).map((t) => ({ ...t, from: "import", scope: "series" as const }))];
      await api.putSeriesGlossary(name, merged);
      s.toast(`已合入 ${merged.length - existing.length} 条术语到作品术语表`);
      setDraft(null);
      setOpen(false);
      onApplied();
    } catch (e) { s.setError((e as Error).message); }
  };

  if (!open) {
    return (
      <div className="row" style={{ marginBottom: 10 }}>
        <button className="btn sm" onClick={() => setOpen(true)}>AI 联网查资料 / 粘贴文本建档…</button>
        <span className="hint">自动查 Bangumi / Wikipedia 或粘贴百科文案，提炼结构化人物表</span>
      </div>
    );
  }
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="row" style={{ marginBottom: 8 }}>
        <b>AI 联网建档 / 粘贴文本整理</b>
        <span className="sp" />
        <button className="btn sm" onClick={() => { setOpen(false); setDraft(null); }}>收起</button>
      </div>
      <div className="row wrap" style={{ gap: 8, marginBottom: 8 }}>
        <input className="inp" style={{ flex: 1, minWidth: 200 }} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="作品名（用于检索 Bangumi / Wikipedia）" />
        <button className="btn pri" disabled={busy || !query.trim()} onClick={() => run(false)}>{busy ? "检索中…" : "联网检索"}</button>
      </div>
      <textarea className="inp" rows={3} style={{ width: "100%", fontFamily: "inherit" }}
                placeholder="或直接粘贴百科 / 官网 / 角色介绍文本，点下方按钮由 AI 整理为结构化人物卡与术语"
                value={pasted} onChange={(e) => setPasted(e.target.value)} />
      <div className="row" style={{ marginTop: 6 }}>
        <span className="hint">所有结果先预览，确认后再写入作品术语表</span>
        <span className="sp" />
        <button className="btn" disabled={busy || !pasted.trim()} onClick={() => run(true)}>{busy ? "整理中…" : "用粘贴文本 AI 整理"}</button>
      </div>
      {draft && (
        <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--bd)" }}>
          <div className="hint" style={{ marginBottom: 4 }}>来源：{draft.sources.join("、") || "无"} · 提取到 {draft.terms.length} 条术语</div>
          {draft.context && <div className="muted" style={{ marginBottom: 6, whiteSpace: "pre-wrap" }}>{draft.context.slice(0, 300)}</div>}
          <div className="row">
            <span className="sp" />
            <button className="btn pri sm" disabled={!draft.terms.length} onClick={apply}>合入作品术语表</button>
          </div>
        </div>
      )}
    </div>
  );
}

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
        <button className={tab === "songs" ? "on" : ""} onClick={() => setTab("songs")}>歌曲库</button>
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
                  <td><b style={{ fontWeight: 500 }}>{e.name}</b>{e.running && <span className="chip acc mini">运行中</span>}{!e.running && e.failed && <span className="chip bad mini" title={briefError(e.failed.error)}>上次失败{e.failed.stage ? `（${STAGE_LBL[e.failed.stage] ?? e.failed.stage}）` : ""}</span>}{!e.exists && <span className="chip bad mini">文件不存在</span>}
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
      {tab === "notes" && <><SeriesResearchBar name={name} onApplied={load} /><SeriesNotes name={name} /></>}
      {tab === "songs" && <SeriesSongsPanel name={name} />}
      {ov.ui}
    </div></div>
  );
}
