import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { Glossary, Reading, Term } from "../types";
import { deepGet, langName, setIn } from "../util";

const TYPES = ["地名", "人名", "团体", "物品", "方言", "其他"];

export function GlossaryPage() {
  const s = useStore();
  const [gl, setGl] = useState<Glossary | null>(null);
  const [terms, setTerms] = useState<Term[]>([]);
  const [readings, setReadings] = useState<Record<string, Reading[]>>({});
  const [series, setSeries] = useState<{ name: string; terms: number; episodes: number }[]>([]);
  const [ctx, setCtx] = useState("");
  const [seriesName, setSeriesName] = useState("");
  const [filter, setFilter] = useState("");
  const [dirty, setDirty] = useState({ terms: false, readings: false, project: false });

  const load = async () => {
    if (!s.pid) return;
    const [g, r, ss] = await Promise.all([api.glossary(s.pid), api.readings(s.pid), api.series()]);
    setGl(g); setTerms(g.terms); setReadings(r); setSeries(ss);
    setCtx(g.context); setSeriesName(g.series);
    setDirty({ terms: false, readings: false, project: false });
  };
  useEffect(() => { load().catch((e) => s.setError(e.message)); }, [s.pid, s.version]);

  if (!gl || !s.data) return <div className="empty">加载中…</div>;
  const translateStale = s.summary?.plan.find((p) => p.stage === "translate")?.status === "stale";
  const shown = terms.map((t, i) => [t, i] as const).filter(([t]) => !filter || [t.src, t.tr, t.reading].some((x) => x?.includes(filter)));
  const updTerm = (i: number, patch: Partial<Term>) => { setTerms(terms.map((t, k) => (k === i ? { ...t, ...patch } : t))); setDirty({ ...dirty, terms: true }); };

  const saveProject = async () => {
    let p = setIn(s.config!.project, "project.context", ctx);
    p = setIn(p, "project.series", seriesName.trim());
    await s.setConfig(p);
    await load();
    s.toast("已保存；节目说明改了需要重新翻译才生效");
  };
  const saveTerms = async () => {
    await api.putGlossary(s.pid!, terms);
    await s.reload();
    s.toast("术语表已保存；重新翻译后生效");
  };
  const saveReadings = async () => {
    await api.putReadings(s.pid!, readings);
    await s.reload();
    s.toast("读音词典已保存，注音已更新");
  };

  return (
    <div className="page"><div className="wide">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>术语与作品</h2>
        <span className="chip">{gl.series ? `作品：${gl.series}（多集共享）` : "未归入作品：术语只在这个项目里用"}</span>
        <span className="sp" />
        {translateStale && <><span className="chip warn">术语 / 说明改了，需要重新翻译</span>
          <button className="btn pri" disabled={s.running} onClick={() => s.run({})}>▶ 重新翻译</button></>}
      </div>
      <div className="grid2" style={{ gridTemplateColumns: ".85fr 1.15fr" }}>
        <div>
          <div className="card">
            <h3>作品</h3>
            <div className="field"><label>所属作品（同一作品的各集共享术语表、读音词典）</label>
              <div className="row">
                <input className="inp" list="series-list" value={seriesName} placeholder="不填 = 不归入作品"
                       onChange={(e) => { setSeriesName(e.target.value); setDirty({ ...dirty, project: true }); }} />
                <datalist id="series-list">{series.map((x) => <option key={x.name} value={x.name}>{x.terms} 条术语 · {x.episodes} 集</option>)}</datalist>
              </div></div>
            <div className="field"><label>节目说明（会告诉大模型）</label>
              <textarea className="inp" rows={4} value={ctx} onChange={(e) => { setCtx(e.target.value); setDirty({ ...dirty, project: true }); }} /></div>
            <div className="row"><span className="sp" /><button className="btn pri" disabled={!dirty.project} onClick={saveProject}>保存</button></div>
            {gl.summary && (<><h3 style={{ marginTop: 10 }}>本集概要 <span className="dim" style={{ fontWeight: 400 }}>（大模型生成）</span></h3>
              <div className="muted" style={{ whiteSpace: "pre-wrap" }}>{gl.summary}</div></>)}
          </div>
          <div className="gap" />
          <div className="card">
            <div className="row" style={{ marginBottom: 8 }}>
              <h3 style={{ margin: 0 }}>读音词典</h3><span className="dim" style={{ fontSize: 12 }}>注音时优先于术语表和分词器</span><span className="sp" />
              <button className="btn pri sm" disabled={!dirty.readings} onClick={saveReadings}>保存</button>
            </div>
            {Object.entries(readings).map(([scope, list]) => (
              <div key={scope} style={{ marginBottom: 10 }}>
                <div className="lbl" style={{ marginBottom: 4 }}>{scope === "series" ? `作品「${gl.series}」` : "全局（所有项目）"}</div>
                <table className="tb"><thead><tr><th>语言</th><th>写法</th><th>读音</th><th /></tr></thead><tbody>
                  {list.map((e, i) => (
                    <tr key={i}>
                      <td><select className="inp" value={e.lang} onChange={(ev) => { setReadings({ ...readings, [scope]: list.map((x, k) => (k === i ? { ...x, lang: ev.target.value } : x)) }); setDirty({ ...dirty, readings: true }); }}>
                        {s.langs.filter((l) => l.annotations.length).map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}</select></td>
                      <td><input className="inp ja" value={e.src} onChange={(ev) => { setReadings({ ...readings, [scope]: list.map((x, k) => (k === i ? { ...x, src: ev.target.value } : x)) }); setDirty({ ...dirty, readings: true }); }} /></td>
                      <td><input className="inp ja" value={e.reading} onChange={(ev) => { setReadings({ ...readings, [scope]: list.map((x, k) => (k === i ? { ...x, reading: ev.target.value } : x)) }); setDirty({ ...dirty, readings: true }); }} /></td>
                      <td><button className="btn sm ghost" onClick={() => { setReadings({ ...readings, [scope]: list.filter((_, k) => k !== i) }); setDirty({ ...dirty, readings: true }); }}>×</button></td>
                    </tr>
                  ))}
                </tbody></table>
                <button className="btn sm" style={{ marginTop: 4 }} onClick={() => { setReadings({ ...readings, [scope]: [...list, { lang: s.data!.src, src: "", reading: "" }] }); setDirty({ ...dirty, readings: true }); }}>＋ 新增</button>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="row" style={{ marginBottom: 8 }}>
            <h3 style={{ margin: 0 }}>术语表</h3>
            <span className="dim">{terms.length} 条 · {langName(s.data.src, s.langs)} → {s.data.targets.map((t) => langName(t, s.langs)).join("、")}</span>
            <span className="sp" />
            <input className="inp" placeholder="搜索…" style={{ width: 150 }} value={filter} onChange={(e) => setFilter(e.target.value)} />
            <button className="btn sm" onClick={() => { setTerms([{ src: "", tr: "", type: "其他", from: "manual" }, ...terms]); setDirty({ ...dirty, terms: true }); }}>＋ 新增</button>
            <button className="btn pri sm" disabled={!dirty.terms} onClick={saveTerms}>保存</button>
          </div>
          <div style={{ maxHeight: "calc(100vh - 220px)", overflow: "auto" }}>
            <table className="tb"><thead><tr><th>原文</th><th>读音</th><th>译法</th><th>类型</th><th>来源</th><th /></tr></thead><tbody>
              {shown.map(([t, i]) => (
                <tr key={i}>
                  <td><input className="inp ja" value={t.src} onChange={(e) => updTerm(i, { src: e.target.value })} /></td>
                  <td><input className="inp ja" value={t.reading ?? ""} onChange={(e) => updTerm(i, { reading: e.target.value })} /></td>
                  <td><input className="inp" value={t.tr ?? ""} onChange={(e) => updTerm(i, { tr: e.target.value })} /></td>
                  <td><select className="inp" value={t.type ?? "其他"} onChange={(e) => updTerm(i, { type: e.target.value })}>{TYPES.map((x) => <option key={x}>{x}</option>)}</select></td>
                  <td><span className={`chip ${t.from === "manual" ? "acc" : t.from === gl.episode ? "" : "ok"}`}>{t.from === "manual" ? "手动" : t.from === gl.episode ? "本集" : t.from ? `第「${t.from}」集` : "继承"}</span></td>
                  <td><button className="btn sm ghost" onClick={() => { setTerms(terms.filter((_, k) => k !== i)); setDirty({ ...dirty, terms: true }); }}>×</button></td>
                </tr>
              ))}
            </tbody></table>
            {terms.length === 0 && <div className="dim" style={{ padding: 12 }}>还没有术语。翻译时大模型会自动整理，也可以手动新增。</div>}
          </div>
          <div className="dim" style={{ fontSize: 12, marginTop: 6 }}>保存在 {gl.path}{deepGet(s.config?.effective, "llm.mode") === "export" ? "；导出任务模式下术语表写在任务文件里" : ""}</div>
        </div>
      </div>
    </div></div>
  );
}
