// 资料：给大模型和注音用的背景资料——节目说明、本集概要、术语表、读音词典。改了自动保存。
import { useState } from "react";
import { api } from "../api";
import { Modal } from "../components/Modal";
import { SaveChip, useDoc, useDraft, useRemote, type SaveState } from "../doc";
import { useStore } from "../store";
import type { Cfg, Glossary, Reading, Term } from "../types";
import { langName, setIn } from "../util";

const TYPES = ["地名", "人名", "团体", "场所", "物品", "方言", "其他"];
type Scope = "all" | "series" | "episode";
/** 人工给的术语（手动加的、导入的）：大模型听音频识别时也参考它们的写法；自动整理的（可能带着识别错字）不参考 */
const userTerm = (t: Term) => t.from === "manual" || t.from === "import";

export const TERMS_FORMAT = "一行一条：原文 | 读音 | 译法 | 类型 | 备注（用 | 或 Tab 分开，后面几项可以空着；# 开头的行不读）";

/** 导入的文字 → 术语。类型不认识的算“其他” */
export function parseTerms(text: string): Term[] {
  const out: Term[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const [src, reading, tr, type, note] = line.split(line.includes("\t") ? "\t" : "|").map((x) => x.trim());
    if (!src) continue;
    out.push({ src, ...(reading ? { reading } : {}), ...(tr ? { tr } : {}), type: TYPES.includes(type ?? "") ? type : "其他",
               ...(note ? { note } : {}), from: "import" });
  }
  return [...new Map(out.map((t) => [t.src, t] as const)).values()];  // 同一个原文写了两遍：用后面那条
}

export function formatTerms(terms: Term[]): string {
  return ["# " + TERMS_FORMAT, ...terms.map((t) => [t.src, t.reading ?? "", t.tr ?? "", t.type ?? "其他", t.note ?? ""].join(" | ").replace(/( \| )+$/, ""))].join("\n");
}

/** 导入：同一个原文已经有的，用导入的覆盖（空着的项保留原来的）；clearAuto：先删掉自动整理的 */
export function importTerms(cur: Term[], add: Term[], clearAuto: boolean): Term[] {
  const base = clearAuto ? cur.filter(userTerm) : [...cur];
  const idx = new Map(base.map((t, i) => [t.src, i] as const));
  const fresh: Term[] = [];
  for (const t of add) {
    const i = idx.get(t.src);
    if (i === undefined) { idx.set(t.src, -1); fresh.push(t); continue; }
    if (i >= 0) base[i] = { ...base[i], ...Object.fromEntries(Object.entries(t).filter(([, v]) => v)) };
  }
  return [...fresh, ...base];
}

function ImportTerms({ terms, onImport, onClose }: { terms: Term[]; onImport: (t: Term[], clearAuto: boolean) => void; onClose: () => void }) {
  const [text, setText] = useState("");
  const [clearAuto, setClearAuto] = useState(false);
  const parsed = parseTerms(text);
  const known = new Set(terms.map((t) => t.src));
  const auto = terms.filter((t) => !userTerm(t)).length;
  return (
    <Modal onClose={onClose} width={720}>
      <h3 style={{ marginTop: 0 }}>导入术语</h3>
      <div className="hint" style={{ marginBottom: 6 }}>{TERMS_FORMAT}。比如：<span className="ja">犬塚信乃 | いぬづか しの | 犬冢信乃 | 人名 | 主人公</span></div>
      <textarea className="inp ja" rows={14} style={{ width: "100%" }} placeholder="把名单粘贴到这里" value={text} onChange={(e) => setText(e.target.value)} />
      <label className="row" style={{ marginTop: 8 }}><input type="checkbox" checked={clearAuto} onChange={(e) => setClearAuto(e.target.checked)} />
        先删掉自动整理的术语（{auto} 条，只留手动加的、导入的）</label>
      <div className="hint">导入的和手动加的术语：翻译时统一译名，选“多模态大模型”识别语音时也照这里的写法写人名；自动整理的只用来统一译名。</div>
      <div className="row" style={{ marginTop: 10 }}>
        <span className="dim">读到 {parsed.length} 条（新增 {parsed.filter((t) => !known.has(t.src)).length}，覆盖 {parsed.filter((t) => known.has(t.src)).length}）</span>
        <span className="sp" />
        <button className="btn" onClick={onClose}>取消</button>
        <button className="btn pri" disabled={!parsed.length} onClick={() => { onImport(parsed, clearAuto); onClose(); }}>导入</button>
      </div>
    </Modal>
  );
}

function ExportTerms({ terms, onClose }: { terms: Term[]; onClose: () => void }) {
  const text = formatTerms(terms);
  return (
    <Modal onClose={onClose} width={720}>
      <h3 style={{ marginTop: 0 }}>导出术语</h3>
      <textarea className="inp ja" rows={16} style={{ width: "100%" }} readOnly value={text} onFocus={(e) => e.target.select()} />
      <div className="row" style={{ marginTop: 10 }}>
        <span className="hint">格式和导入一样：可以复制出去改好再导入回来。</span><span className="sp" />
        <button className="btn" onClick={() => navigator.clipboard?.writeText(text)}>复制</button>
        <button className="btn pri" onClick={onClose}>关闭</button>
      </div>
    </Modal>
  );
}

type Remote<T> = { v: T | null; update: (f: (cur: T) => T) => void; state: SaveState };

/** 一集的资料 */
export function NotesPage() {
  const s = useStore();
  const doc = useDoc();
  const pid = s.pid;
  const ctx = useDraft(doc, (d) => String(d.raw.project?.context ?? d.effective.project?.context ?? ""), (raw, v) => setIn(raw, "project.context", v));
  const gl = useRemote<Glossary>(() => api.glossary(pid!), async (g) => { await api.putGlossary(pid!, g.terms); await s.reload(); }, [pid], s.setError);
  const rd = useRemote<Record<string, Reading[]>>(() => api.readings(pid!), (r) => api.putReadings(pid!, r), [pid], s.setError);
  if (!pid || ctx.v === null) return <div className="empty">加载中…</div>;
  return <NotesView context={ctx.v} setContext={(v) => ctx.update(() => v)} ctxState={ctx.state} gl={gl} rd={rd} episode
                    hint="写节目类型、故事背景、简介、说话的特点，大模型翻译时会参考。人名、地名这类名单放进旁边的术语表（可以导入），不用写在这里。改了下次运行会重新翻译。" />;
}

/** 作品的资料：作品默认设置里的节目说明（新建的集复制过去）、各集共用的术语表和读音词典 */
export function SeriesNotes({ name }: { name: string }) {
  const s = useStore();
  const raw = useRemote<Cfg>(() => api.seriesDoc(name).then((d) => d.raw), (r) => api.putSeries(name, r), [name], s.setError);
  const gl = useRemote<Glossary>(() => api.seriesGlossary(name), (g) => api.putSeriesGlossary(name, g.terms), [name], s.setError);
  const rd = useRemote<Record<string, Reading[]>>(() => api.seriesReadings(name), (r) => api.putSeriesReadings(name, r), [name], s.setError);
  if (!raw.v) return <div className="empty">加载中…</div>;
  return <NotesView context={String(raw.v.project?.context ?? "")} setContext={(v) => raw.update((r) => setIn(r, "project.context", v))} ctxState={raw.state}
                    gl={gl} rd={rd} episode={false} hint="作品默认设置里的节目说明（故事背景、简介）：新建这部作品的集时复制过去，已有的集各自在「资料」里改。人名、地名这类名单放进旁边的术语表（可以导入），各集共用。" />;
}

function NotesView({ context, setContext, ctxState, gl, rd, episode, hint }: {
  context: string; setContext: (v: string) => void; ctxState: SaveState; gl: Remote<Glossary>; rd: Remote<Record<string, Reading[]>>; episode: boolean; hint: string;
}) {
  const s = useStore();
  const [tab, setTab] = useState<"terms" | "readings">("terms");
  const [scope, setScope] = useState<Scope>("all");
  const [filter, setFilter] = useState("");
  const [dialog, setDialog] = useState<"" | "import" | "export">("");
  if (!gl.v || !rd.v) return <div className="empty">加载中…</div>;
  const g = gl.v;
  const series = g.series;
  // 范围：相对这一集（本集整理的 / 以前各集积累的）；不属于作品的项目，手动加的也算本集
  const mine = (t: Term) => t.from === g.episode || (!series && userTerm(t));
  // 来源：哪一集整理出来的（集名只取「」里的标题，全名在提示里）
  const origin = (t: Term) => (t.from === "manual" ? "手动" : t.from === "import" ? "导入" : t.from ? (t.from.match(/「([^」]+)」/)?.[1] ?? t.from) : "—");
  const terms = g.terms.map((t, i) => [t, i] as const)
    .filter(([t]) => scope === "all" || (scope === "episode" ? mine(t) : !mine(t)))
    .filter(([t]) => !filter || [t.src, t.tr, t.reading].some((x) => x?.includes(filter)));
  const updTerm = (i: number, patch: Partial<Term>) => gl.update((x) => ({ ...x, terms: x.terms.map((t, k) => (k === i ? { ...t, ...patch } : t)) }));
  const readings = Object.entries(rd.v).flatMap(([sc, list]) => list.map((e, i) => ({ sc, i, e })))
    .filter(({ e }) => !filter || [e.src, e.reading].some((x) => x?.includes(filter)));
  const updReading = (sc: string, i: number, patch: Partial<Reading>) => rd.update((x) => ({ ...x, [sc]: x[sc].map((e, k) => (k === i ? { ...e, ...patch } : e)) }));
  const moveReading = (sc: string, i: number, to: string) => rd.update((x) => ({ ...x, [sc]: x[sc].filter((_, k) => k !== i), [to]: [...(x[to] ?? []), x[sc][i]] }));
  const annLangs = s.langs.filter((l) => l.annotations.length);
  const saving = [ctxState, gl.state, rd.state].find((x) => x !== "saved") ?? "saved";
  const translateStale = s.summary?.plan.find((p) => p.stage === "translate")?.status === "stale";
  return (
    <div className="page"><div className="wide">
      <div className="row" style={{ marginBottom: 12 }}>
        <span className="chip">{series ? `作品「${series}」：术语表、读音词典各集共用` : "不属于作品：术语表只在这一集里用"}</span>
        {episode && translateStale && <span className="chip warn">节目说明 / 术语改了：下次运行会重新翻译</span>}
        <span className="sp" /><SaveChip state={saving} />
      </div>
      <div className="notes-grid">
        <div className="col">
          <div className="card">
            <h3>节目说明 <span className="dim" style={{ fontWeight: 400 }}>会告诉大模型</span></h3>
            <textarea className="inp" rows={5} placeholder="例如：北海道渔村纪录片，有大量老渔民方言采访" value={context} onChange={(e) => setContext(e.target.value)} />
            <div className="hint" style={{ marginTop: 6 }}>{hint}</div>
          </div>
          {episode && g.summary && (
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
            {tab === "terms" && series && episode && (<>
              <span className="dim">范围</span>
              <div className="seg">{([["all", "全部"], ["series", "作品共享"], ["episode", "本集新增"]] as const).map(([v, l]) =>
                <button key={v} className={scope === v ? "on" : ""} onClick={() => setScope(v)}>{l}</button>)}</div>
            </>)}
            <span className="sp" />
            <input className="inp" placeholder="搜索…" style={{ width: 140 }} value={filter} onChange={(e) => setFilter(e.target.value)} />
            <button className="btn sm" onClick={() => tab === "terms"
              ? gl.update((x) => ({ ...x, terms: [{ src: "", tr: "", type: "其他", from: "manual" }, ...x.terms] }))
              : rd.update((x) => { const sc = series ? "series" : "global"; return { ...x, [sc]: [{ lang: s.data?.src ?? "ja", src: "", reading: "" }, ...(x[sc] ?? [])] }; })}>＋ 新增</button>
            {tab === "terms" && <button className="btn sm" onClick={() => setDialog("import")}>导入</button>}
            {tab === "terms" && <button className="btn sm" disabled={!g.terms.length} onClick={() => setDialog("export")}>导出</button>}
          </div>
          {tab === "terms" ? (<>
            <div className="scroll-table">
              <table className="tb"><thead><tr><th>原文</th><th>读音</th><th>译法</th><th>类型</th><th>范围</th><th>来源</th><th /></tr></thead><tbody>
                {terms.map(([t, i]) => (
                  <tr key={i}>
                    <td><input className="inp ja" value={t.src} onChange={(e) => updTerm(i, { src: e.target.value })} /></td>
                    <td><input className="inp ja" value={t.reading ?? ""} onChange={(e) => updTerm(i, { reading: e.target.value })} /></td>
                    <td><input className="inp" value={t.tr ?? ""} onChange={(e) => updTerm(i, { tr: e.target.value })} /></td>
                    <td><select className="inp" value={t.type ?? "其他"} onChange={(e) => updTerm(i, { type: e.target.value })}>{TYPES.map((x) => <option key={x}>{x}</option>)}</select></td>
                    <td><span className={`scope ${series && !mine(t) ? "s" : "e"}`}>{mine(t) ? "本集" : series ? "作品" : "继承"}</span></td>
                    <td><span className="origin selectable" title={t.from === "manual" ? "手动加的" : t.from === "import" ? "导入的" : t.from ? `${t.from} 这一集整理出来的` : "以前的，没有记录"}>{origin(t)}</span></td>
                    <td><button className="btn sm ghost" onClick={() => gl.update((x) => ({ ...x, terms: x.terms.filter((_, k) => k !== i) }))}>×</button></td>
                  </tr>
                ))}
              </tbody></table>
              {g.terms.length === 0 && <div className="dim" style={{ padding: 12 }}>还没有术语。翻译时大模型会自动整理，也可以手动新增。</div>}
            </div>
            <div className="hint" style={{ marginTop: 6 }}>保存在 {g.path}；改了下次运行会重新翻译。手动加的、导入的术语，选“多模态大模型”识别语音时也会参考（改了会重新识别）。</div>
            {dialog === "import" && <ImportTerms terms={g.terms} onClose={() => setDialog("")}
              onImport={(add, clearAuto) => { gl.update((x) => ({ ...x, terms: importTerms(x.terms, add, clearAuto) })); s.toast(`已导入 ${add.length} 条术语`); }} />}
            {dialog === "export" && <ExportTerms terms={g.terms} onClose={() => setDialog("")} />}
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
