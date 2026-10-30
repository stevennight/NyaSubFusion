// 资料：给大模型和注音用的背景资料——节目说明、本集概要、术语表、读音词典。改了自动保存。
import { useState } from "react";
import { api } from "../api";
import { SaveChip, useDoc, useDraft, useRemote } from "../doc";
import { useStore } from "../store";
import type { Glossary, Reading, Term } from "../types";
import { langName, setIn } from "../util";

const TYPES = ["地名", "人名", "团体", "物品", "方言", "其他"];
type Scope = "all" | "series" | "episode";

export function NotesPage() {
  const s = useStore();
  const doc = useDoc();
  const pid = s.pid;
  const ctx = useDraft(doc, (d) => String(d.raw.project?.context ?? d.effective.project?.context ?? ""), (raw, v) => setIn(raw, "project.context", v));
  const gl = useRemote<Glossary>(() => api.glossary(pid!), async (g) => { await api.putGlossary(pid!, g.terms); await s.reload(); }, [pid], s.setError);
  const rd = useRemote<Record<string, Reading[]>>(() => api.readings(pid!), (r) => api.putReadings(pid!, r), [pid], s.setError);
  const [tab, setTab] = useState<"terms" | "readings">("terms");
  const [scope, setScope] = useState<Scope>("all");
  const [filter, setFilter] = useState("");
  if (!pid || !gl.v || !rd.v || ctx.v === null) return <div className="empty">加载中…</div>;
  const g = gl.v;
  const series = g.series;
  const mine = (t: Term) => t.from === g.episode;
  const terms = g.terms.map((t, i) => [t, i] as const)
    .filter(([t]) => scope === "all" || (scope === "episode" ? mine(t) : !mine(t)))
    .filter(([t]) => !filter || [t.src, t.tr, t.reading].some((x) => x?.includes(filter)));
  const updTerm = (i: number, patch: Partial<Term>) => gl.update((x) => ({ ...x, terms: x.terms.map((t, k) => (k === i ? { ...t, ...patch } : t)) }));
  const readings = Object.entries(rd.v).flatMap(([sc, list]) => list.map((e, i) => ({ sc, i, e })))
    .filter(({ e }) => !filter || [e.src, e.reading].some((x) => x?.includes(filter)));
  const updReading = (sc: string, i: number, patch: Partial<Reading>) => rd.update((x) => ({ ...x, [sc]: x[sc].map((e, k) => (k === i ? { ...e, ...patch } : e)) }));
  const moveReading = (sc: string, i: number, to: string) => rd.update((x) => ({ ...x, [sc]: x[sc].filter((_, k) => k !== i), [to]: [...(x[to] ?? []), x[sc][i]] }));
  const annLangs = s.langs.filter((l) => l.annotations.length);
  const saving = [ctx.state, gl.state, rd.state].find((x) => x !== "saved") ?? "saved";
  const translateStale = s.summary?.plan.find((p) => p.stage === "translate")?.status === "stale";
  return (
    <div className="page"><div className="wide">
      <div className="row" style={{ marginBottom: 12 }}>
        <span className="chip">{series ? `作品「${series}」：术语表、读音词典各集共用` : "不属于作品：术语表只在这一集里用"}</span>
        {translateStale && <span className="chip warn">节目说明 / 术语改了：下次运行会重新翻译</span>}
        <span className="sp" /><SaveChip state={saving} />
      </div>
      <div className="notes-grid">
        <div className="col">
          <div className="card">
            <h3>节目说明 <span className="dim" style={{ fontWeight: 400 }}>会告诉大模型</span></h3>
            <textarea className="inp" rows={5} placeholder="例如：北海道渔村纪录片，有大量老渔民方言采访" value={ctx.v} onChange={(e) => ctx.update(() => e.target.value)} />
            <div className="hint" style={{ marginTop: 6 }}>写节目类型、出场的人、说话的特点，大模型翻译时会参考。改了下次运行会重新翻译。</div>
          </div>
          {g.summary && (
            <div className="card">
              <h3>本集概要 <span className="dim" style={{ fontWeight: 400 }}>大模型生成，翻译时参考</span></h3>
              <div className="muted" style={{ whiteSpace: "pre-wrap" }}>{g.summary}</div>
            </div>
          )}
        </div>
        <div className="card">
          <div className="row wrap" style={{ marginBottom: 10 }}>
            <div className="seg">
              <button className={tab === "terms" ? "on" : ""} onClick={() => setTab("terms")}>术语表 {g.terms.length}</button>
              <button className={tab === "readings" ? "on" : ""} onClick={() => setTab("readings")}>读音词典 {Object.values(rd.v).reduce((n, l) => n + l.length, 0)}</button>
            </div>
            {tab === "terms" && series && (<>
              <span className="dim">范围</span>
              <div className="seg">{([["all", "全部"], ["series", "作品共享"], ["episode", "本集新增"]] as const).map(([v, l]) =>
                <button key={v} className={scope === v ? "on" : ""} onClick={() => setScope(v)}>{l}</button>)}</div>
            </>)}
            <span className="sp" />
            <input className="inp" placeholder="搜索…" style={{ width: 140 }} value={filter} onChange={(e) => setFilter(e.target.value)} />
            <button className="btn sm" onClick={() => tab === "terms"
              ? gl.update((x) => ({ ...x, terms: [{ src: "", tr: "", type: "其他", from: "manual" }, ...x.terms] }))
              : rd.update((x) => { const sc = series ? "series" : "global"; return { ...x, [sc]: [{ lang: s.data?.src ?? "ja", src: "", reading: "" }, ...(x[sc] ?? [])] }; })}>＋ 新增</button>
          </div>
          {tab === "terms" ? (<>
            <div className="scroll-table">
              <table className="tb"><thead><tr><th>原文</th><th>读音</th><th>译法</th><th>类型</th><th>{series ? "范围" : "来源"}</th><th /></tr></thead><tbody>
                {terms.map(([t, i]) => (
                  <tr key={i}>
                    <td><input className="inp ja" value={t.src} onChange={(e) => updTerm(i, { src: e.target.value })} /></td>
                    <td><input className="inp ja" value={t.reading ?? ""} onChange={(e) => updTerm(i, { reading: e.target.value })} /></td>
                    <td><input className="inp" value={t.tr ?? ""} onChange={(e) => updTerm(i, { tr: e.target.value })} /></td>
                    <td><select className="inp" value={t.type ?? "其他"} onChange={(e) => updTerm(i, { type: e.target.value })}>{TYPES.map((x) => <option key={x}>{x}</option>)}</select></td>
                    <td><span className={`scope ${series && !mine(t) ? "s" : "e"}`} title={t.from === "manual" ? "手动加的" : t.from ? `第「${t.from}」集整理出来的` : "以前的"}>
                      {series ? (mine(t) ? "本集" : "作品") : ""}{t.from === "manual" ? " · 手动" : !series ? (mine(t) ? "本集" : "继承") : ""}</span></td>
                    <td><button className="btn sm ghost" onClick={() => gl.update((x) => ({ ...x, terms: x.terms.filter((_, k) => k !== i) }))}>×</button></td>
                  </tr>
                ))}
              </tbody></table>
              {g.terms.length === 0 && <div className="dim" style={{ padding: 12 }}>还没有术语。翻译时大模型会自动整理，也可以手动新增。</div>}
            </div>
            <div className="hint" style={{ marginTop: 6 }}>保存在 {g.path}；改了下次运行会重新翻译。</div>
          </>) : (<>
            <div className="scroll-table">
              <table className="tb"><thead><tr><th>写法</th><th>读音</th><th>语言</th><th>范围</th><th /></tr></thead><tbody>
                {readings.map(({ sc, i, e }) => (
                  <tr key={`${sc}:${i}`}>
                    <td><input className="inp ja" value={e.src} onChange={(ev) => updReading(sc, i, { src: ev.target.value })} /></td>
                    <td><input className="inp ja" value={e.reading} onChange={(ev) => updReading(sc, i, { reading: ev.target.value })} /></td>
                    <td><select className="inp" value={e.lang} onChange={(ev) => updReading(sc, i, { lang: ev.target.value })}>
                      {annLangs.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}</select></td>
                    <td>{series ? <select className="inp" value={sc} onChange={(ev) => moveReading(sc, i, ev.target.value)}>
                      <option value="series">作品</option><option value="global">全局</option></select> : <span className="scope g">全局</span>}</td>
                    <td><button className="btn sm ghost" onClick={() => rd.update((x) => ({ ...x, [sc]: x[sc].filter((_, k) => k !== i) }))}>×</button></td>
                  </tr>
                ))}
              </tbody></table>
              {readings.length === 0 && <div className="dim" style={{ padding: 12 }}>还没有读音。审校时点汉字改读音，也会存到这里。</div>}
            </div>
            <div className="hint" style={{ marginTop: 6 }}>注音时：读音词典 &gt; 术语表的读音 &gt; 分词器。“全局”对所有项目有效{series ? `，“作品”只对「${series}」的各集有效` : ""}。没填读音的不会保存。语言：{langName(s.data?.src ?? "", s.langs)}。</div>
          </>)}
        </div>
      </div>
    </div></div>
  );
}
