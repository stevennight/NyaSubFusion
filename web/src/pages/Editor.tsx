import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Player, type PlayerHandle } from "../components/Player";
import { Timeline } from "../components/Timeline";
import { usePeaks, usePreview } from "../hooks";
import { useStore } from "../store";
import type { Candidate, Cue, EditResult, Word } from "../types";
import { FLAG_LBL, fmt, langName, SRC_LBL, TYPE_LBL } from "../util";

type Filter = "all" | "todo" | "asr" | "ocr";

export function EditorPage() {
  const s = useStore();
  const d = s.data;
  const [profile, setProfile] = useState<string | undefined>(() => localStorage.getItem("nyasub.edProfile") ?? undefined);
  const preview = usePreview(profile);
  const peaks = usePeaks();
  const player = useRef<PlayerHandle>(null);
  const [time, setTime] = useState(0);
  const [sel, setSel] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);   // 鼠标停在合并按钮上时，高亮将被合并进来的那一条
  const [filter, setFilter] = useState<Filter>("all");
  const [showBoxes, setShowBoxes] = useState(false);
  const [pps, setPps] = useState(26);
  const onTime = useCallback((t: number) => setTime(t), []);

  useEffect(() => { if (profile) localStorage.setItem("nyasub.edProfile", profile); }, [profile]);
  const cues = useMemo(() => d?.cues ?? [], [d]);
  const duration = d?.meta?.duration ?? 0;
  const target = d?.targets[0] ?? "zh-Hans";

  const select = useCallback((id: string, seek = true) => {
    setSel(id);
    const c = cues.find((x) => x.id === id);
    if (c && seek) player.current?.seek(c.start + Math.min(0.5, (c.end - c.start) / 3));
  }, [cues]);
  // 结构修改（拆分 / 合并 / 插入 / 撤销 / 重做）：做完重新加载，选中结果条目
  const edit = useCallback(async (fn: () => Promise<EditResult>, msg?: string) => {
    try {
      const r = await fn();
      await s.reload();
      if (r.cue) setSel(r.cue.id);
      if (msg) s.toast(msg);
    } catch (e) { s.setError((e as Error).message); }
  }, [s]);
  const undo = useCallback(() => s.pid && edit(() => api.undoRedo(s.pid!, "undo"), "已撤销"), [s.pid, edit]);
  const redo = useCallback(() => s.pid && edit(() => api.undoRedo(s.pid!, "redo"), "已重做"), [s.pid, edit]);
  const insertAt = useCallback(() => s.pid && edit(() => api.insertCue(s.pid!, { start: Math.round(time * 100) / 100, end: Math.round((time + 2) * 100) / 100 }), "已插入一条空字幕"), [s.pid, edit, time]);

  const step = useCallback((dir: number) => {
    const i = cues.findIndex((c) => c.id === sel);
    const n = cues[Math.max(0, Math.min(cues.length - 1, i + dir))];
    if (n) select(n.id);
  }, [cues, sel, select]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.closest?.("input,textarea,select")) return;
      if (e.code === "Space") { e.preventDefault(); player.current?.toggle(); }
      if (e.key === "j" || e.key === "J") step(-1);
      if (e.key === "k" || e.key === "K") step(1);
      if (e.ctrlKey && (e.key === "z" || e.key === "Z") && !e.shiftKey) { e.preventDefault(); undo(); }
      if (e.ctrlKey && (e.key === "y" || e.key === "Y" || ((e.key === "z" || e.key === "Z") && e.shiftKey))) { e.preventDefault(); redo(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, undo, redo]);

  if (!d) return <div className="empty">加载中…</div>;
  if (!cues.length) return <div className="empty">还没有字幕。先在「流程」里运行一次。</div>;

  const matches = (c: Cue) => filter === "all" || (filter === "todo" && c.review === "todo")
    || (filter === "asr" && c.flags.includes("asr_only")) || (filter === "ocr" && c.sources.some((x) => x.startsWith("o")));
  const counts: Record<Filter, number> = { all: cues.length, todo: 0, asr: 0, ocr: 0 };
  for (const c of cues) for (const f of ["todo", "asr", "ocr"] as Filter[]) if (f !== "all" && ((f === "todo" && c.review === "todo") || (f === "asr" && c.flags.includes("asr_only")) || (f === "ocr" && c.sources.some((x) => x.startsWith("o"))))) counts[f]++;
  const current = new Set(cues.filter((c) => time >= c.start && time < c.end).map((c) => c.id));
  const selected = cues.find((c) => c.id === sel) ?? null;

  return (
    <div className="editor">
      <div className="player">
        <Player ref={player} preview={preview} boxes={showBoxes ? d.ocr : undefined} onTime={onTime} />
        <div className="pctl">
          <button className="iconbtn" title="上一条 (J)" onClick={() => step(-1)}>⏮</button>
          <button className="iconbtn big" title="播放 / 暂停（空格）" onClick={() => player.current?.toggle()}>▶</button>
          <button className="iconbtn" title="下一条 (K)" onClick={() => step(1)}>⏭</button>
          <span className="time">{fmt(time)} / {fmt(duration)}</span>
          <label className="row dim"><input type="checkbox" checked={showBoxes} onChange={(e) => setShowBoxes(e.target.checked)} />识别框</label>
          <span className="sp" />
          预览方案
          <select className="inp" style={{ width: "auto" }} value={preview?.profile ?? ""} onChange={(e) => setProfile(e.target.value)}>
            {(preview?.profiles ?? []).map((p) => <option key={p}>{p}</option>)}
          </select>
        </div>
      </div>
      <Timeline duration={duration} time={time} pps={pps} subs={d.subs} ocr={d.ocr} asr={d.asr} fused={d.fused.items} cues={cues}
                target={target} peaks={peaks} selected={sel} hint={hint} onSeek={(t) => player.current?.seek(t)} onSelect={(id) => select(id)}
                onDropped={(c: Candidate) => { s.toast(`${c.id}「${c.text}」${c.dropped ? `已丢弃（${c.dropped}）` : c.dup_of ? "与字幕重复，已去掉" : "没有用到"}`); player.current?.seek(c.start); }} />
      <div className="cuelist">
        <div className="hd">
          <b>字幕</b>
          <div className="seg">{([["all", "全部"], ["todo", "待审校"], ["asr", "仅语音"], ["ocr", "含画面字"]] as [Filter, string][]).map(([f, l]) =>
            <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>{l} {counts[f]}</button>)}</div>
          <span className="sp" />
          <span className="dim">缩放</span>
          <input type="range" min={4} max={80} value={pps} onChange={(e) => setPps(Number(e.target.value))} style={{ maxWidth: 120 }} />
          <button className="btn sm" title="在播放头处插入一条 2 秒的空字幕" onClick={insertAt}>＋ 插入</button>
          <button className="btn sm" title="撤销（Ctrl+Z）" disabled={!d.history?.undo} onClick={undo}>↶ 撤销</button>
          <button className="btn sm" title="重做（Ctrl+Y）" disabled={!d.history?.redo} onClick={redo}>↷ 重做</button>
          <span className="dim">J/K 上下条 · 空格播放</span>
        </div>
        <div className="bd">
          <table className="tb"><thead><tr><th /><th>#</th><th>时间</th><th>类型</th><th>来源</th><th>原文</th><th>译文</th><th>问题</th></tr></thead>
            <tbody>
              {cues.filter(matches).map((c) => (
                <tr key={c.id} className={`${c.id === sel ? "sel" : ""} ${current.has(c.id) ? "cur" : ""} ${c.id === hint ? "hint" : ""}`} onClick={() => select(c.id)}>
                  <td><span className={`dot ${c.review}`} /></td><td className="dim">{cues.indexOf(c) + 1}</td>
                  <td className="t">{fmt(c.start)} → {fmt(c.end)}</td><td className="typ">{TYPE_LBL[c.type] ?? c.type}</td>
                  <td>{c.sources.map((x) => <span key={x} className={`badge b-${x[0]}`} style={{ marginRight: 3 }}>{SRC_LBL[x[0]]}</span>)}</td>
                  <td className="ja"><Pieces cue={c} text={c.orig.text} /></td><td><Pieces cue={c} text={c.tr[target]?.text ?? ""} /></td>
                  <td className="fl">{c.flags.map((f) => FLAG_LBL[f] ?? f).join(" · ")}</td>
                </tr>
              ))}
            </tbody></table>
        </div>
      </div>
      <aside className="inspector">
        {selected ? <Inspector key={selected.id + JSON.stringify([selected.tr, selected.orig.text, selected.start, selected.end, selected.parts])} cue={selected} index={cues.indexOf(selected)} onStep={step}
                               time={time} prev={cues[cues.indexOf(selected) - 1]} next={cues[cues.indexOf(selected) + 1]} target={target} onHint={setHint}
                               onSplit={() => edit(() => api.splitCue(s.pid!, selected.id, time), "已拆成两条")}
                               onPieces={(body) => edit(() => api.cuePieces(s.pid!, selected.id, body),
                                 body.action === "join" ? "已合成一段" : body.action === "cut" ? "已分成两段" : body.at === "all" ? "已全部拆成单独的字幕" : "已拆成两条字幕")}
                               onMergeUp={() => { const p = cues[cues.indexOf(selected) - 1]; setHint(null); edit(() => api.mergeNext(s.pid!, p.id), `已和上面的 #${cues.indexOf(selected)} 合并`); }}
                               onMergeDown={() => { setHint(null); edit(() => api.mergeNext(s.pid!, selected.id), `已和下面的 #${cues.indexOf(selected) + 2} 合并`); }} />
          : <div className="isec dim">点时间轴或列表里的一条字幕，在这里对照来源、修改原文 / 译文 / 读音。</div>}
      </aside>
    </div>
  );
}

/** 用“｜”分段的字幕：每段一行显示（输出时每段单独一屏）。 */
function Pieces({ cue, text }: { cue: Cue; text: string }) {
  const ps = text.split("｜").filter((p) => p.trim());
  if (!cue.parts || ps.length < 2) return <>{text.replace(/｜/g, "")}</>;
  return <>{ps.map((p, i) => <div key={i} className="piece">{p}</div>)}</>;
}

type PieceAction = { action: "split" | "join"; at: number | "all" } | { action: "cut"; t: number };

const splitBars = (text: string) => text.split("｜").filter((p) => p.trim());

function Inspector({ cue, index, onStep, time, prev, next, target, onHint, onSplit, onPieces, onMergeUp, onMergeDown }: {
  cue: Cue; index: number; onStep: (d: number) => void; time: number; prev?: Cue; next?: Cue; target: string;
  onHint: (id: string | null) => void; onSplit: () => void; onPieces: (body: PieceAction) => void;
  onMergeUp: () => void; onMergeDown: () => void;
}) {
  // 合并按钮的提示：和哪一条合并、合并后的时间和文字
  const mergeTip = (a: Cue, b: Cue) =>
    `合并成一条：${fmt(a.start)} → ${fmt(b.end)}
原文：${a.orig.text} ／ ${b.orig.text}
译文：${a.tr[target]?.text ?? ""} ／ ${b.tr[target]?.text ?? ""}`;
  const canSplit = time > cue.start + 0.2 && time < cue.end - 0.2;
  const s = useStore();
  const d = s.data!;
  // 每段：时间 + 原文 + 各译文。没分段的字幕就是一段
  const multi = !!cue.parts && cue.parts.length > 1;
  const segs: [number, number][] = multi ? cue.parts! : [[cue.start, cue.end]];
  const fields = ["orig", ...d.targets];
  const fullText = (f: string) => (f === "orig" ? cue.orig.text : cue.tr[f]?.text ?? "");
  const piecesOf = (f: string) => {
    if (!multi) return [fullText(f)];
    const ps = splitBars(fullText(f));
    return segs.map((_, i) => ps[i] ?? "");
  };
  const [texts, setTexts] = useState<Record<string, string[]>>(() => Object.fromEntries(fields.map((f) => [f, piecesOf(f)])));
  const [alts, setAlts] = useState<{ orig: string; tr: string }[] | null>(null);
  const [instr, setInstr] = useState("");
  const [busy, setBusy] = useState(false);
  const [pop, setPop] = useState<{ wi: number; si: number; x: number; y: number; value: string } | null>(null);
  const items = Object.fromEntries(d.fused.items.map((it) => [it.id, it]));
  const words: Word[] = d.tokens[cue.id]?.orig ?? [];
  const curSeg = segs.findIndex(([a, b]) => time >= a && time < b);
  const canCut = curSeg >= 0 && time > segs[curSeg][0] + 0.2 && time < segs[curSeg][1] - 0.2;

  const patch = async (body: Record<string, unknown>, msg?: string) => {
    if (!s.pid) return;
    setBusy(true);
    try { await api.editCue(s.pid, cue.id, body); await s.reload(); if (msg) s.toast(msg); }
    catch (e) { s.setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const saveText = (f: string) => {
    const joined = multi ? texts[f].map((p) => p.trim()).join("｜") : texts[f][0];
    if (joined === fullText(f)) return;
    patch(f === "orig" ? { orig: joined } : { tr: { [f]: joined } }, "已保存");
  };
  const setPiece = (f: string, i: number, v: string) => setTexts({ ...texts, [f]: texts[f].map((p, k) => (k === i ? v : p)) });
  // 改某段的开始 / 结束：相邻那段跟着动；第一段的开始、最后一段的结束就是整条字幕的起止
  const saveTime = (i: number, edge: 0 | 1, v: number) => {
    if (!Number.isFinite(v) || Math.abs(v - segs[i][edge]) < 0.006) return;  // 输入框里显示的是两位小数
    const ps = segs.map(([a, b]) => [a, b] as [number, number]);
    ps[i][edge] = v;
    if (edge === 0 && i > 0) ps[i - 1][1] = v;
    if (edge === 1 && i < ps.length - 1) ps[i + 1][0] = v;
    if (ps.some(([a, b]) => b - a < 0.2)) { s.setError("每段至少要 0.2 秒，而且不能和相邻的段颠倒"); return; }
    patch({ start: ps[0][0], end: ps[ps.length - 1][1], ...(multi ? { parts: ps } : {}) }, "时间已保存");
  };
  const retranslate = async () => {
    if (!s.pid) return;
    setBusy(true);
    setAlts([]);
    try { setAlts((await api.retranslate(s.pid, cue.id, { instruction: instr })).alts); }
    catch (e) { setAlts(null); s.setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const saveReading = async () => {
    if (!pop || !s.pid) return;
    const w = words[pop.wi];
    // 读音词典按整词记：未改的段沿用原读音
    const reading = w.segs.map((sg, i) => (i === pop.si ? pop.value : sg[1] ?? sg[0])).join("");
    setPop(null);
    try { await api.addReading(s.pid, { lang: d.src, src: w.t, reading }); await s.reload(); s.toast(`已记入读音词典：${w.t} → ${reading}`); }
    catch (e) { s.setError((e as Error).message); }
  };

  return (
    <>
      <div className="isec">
        <div className="row" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>#{index + 1}</h3><span className={`dot ${cue.review}`} /><span className="dim">{cue.review === "ok" ? "已审" : "待审校"}</span>
          <span className="sp" /><button className="btn sm" onClick={() => onStep(-1)}>↑</button><button className="btn sm" onClick={() => onStep(1)}>↓</button>
        </div>
        <div className="kv" style={{ gridTemplateColumns: "44px 1fr" }}>
          <span className="lbl">时间</span>
          <span className="dim">{fmt(cue.start)} → {fmt(cue.end)} · {(cue.end - cue.start).toFixed(1)}s{multi ? ` · 分 ${segs.length} 段显示` : ""}</span>
          <span className="lbl">类型</span>
          <select className="inp" value={cue.type} onChange={(e) => patch({ type: e.target.value })}>
            {Object.entries(TYPE_LBL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          {["info", "name", "title"].includes(cue.type) && (() => {
            // 说明类显示在哪：默认跟着最主要的那条画面字；识别错了可以改成跟着另一条，或者放到画面顶部
            const TOP = [0.3, 0.01, 0.7, 0.05];
            const same = (a?: number[], b?: number[]) => JSON.stringify(a) === JSON.stringify(b);
            const srcs = cue.sources.map((id) => items[id]).filter((it) => it?.rect);
            const value = same(cue.rect, TOP) ? "top" : srcs.find((it) => same(it.rect, cue.rect))?.id ?? "auto";
            return (<>
              <span className="lbl">位置</span>
              <select className="inp" value={value} title="显示在画面上哪里（输出时译文贴在这处画面字旁边）"
                      onChange={(e) => {
                        const v = e.target.value;
                        patch({ rect: v === "auto" ? null : v === "top" ? TOP : items[v].rect }, "位置已更改");
                      }}>
                <option value="auto">自动（跟着最主要的画面字）</option>
                {srcs.map((it) => <option key={it.id} value={it.id}>跟着 {it.id}：{it.text.replace(/\n/g, " ").slice(0, 16)}</option>)}
                <option value="top">画面顶部</option>
              </select>
            </>);
          })()}
        </div>
        {cue.flags.length > 0 && <div className="row wrap" style={{ marginTop: 8 }}>{cue.flags.map((f) => <span key={f} className="chip warn">{FLAG_LBL[f] ?? f}</span>)}</div>}
      </div>

      <div className="isec">
        <div className="row" style={{ marginBottom: 6 }}>
          <h3 style={{ margin: 0 }}>字幕 <span className="dim" style={{ fontWeight: 400 }}>{multi ? `${segs.length} 段，输出时依次显示` : "1 段"}</span></h3>
          <span className="sp" />
          <button className="btn sm" disabled={busy || !canCut} title={canCut ? `在 ${fmt(time)} 处把第 ${curSeg + 1} 段分成两段（还是同一条字幕，文字按时间比例切开，之后可以改）` : "把播放头移到某一段中间"}
                  onClick={() => onPieces({ action: "cut", t: time })}>✂ 在播放头分段</button>
          {multi && <button className="btn sm" disabled={busy} title="每段拆成一条单独的字幕" onClick={() => onPieces({ action: "split", at: "all" })}>全部拆成单独字幕</button>}
        </div>
        {segs.map(([a, b], i) => (
          <div key={i}>
            {i > 0 && (
              <div className="piece-gap">
                <button className="btn sm" disabled={busy} title={`从 ${fmt(a)} 处拆成两条单独的字幕`} onClick={() => onPieces({ action: "split", at: i })}>✂ 拆成两条字幕</button>
                <button className="btn sm" disabled={busy} title={`第 ${i} 段和第 ${i + 1} 段合成一段（一屏）`} onClick={() => onPieces({ action: "join", at: i })}>⤒ 和上一段合并</button>
              </div>
            )}
            <div className={`piece-card ${i === curSeg ? "cur" : ""}`}>
              <div className="row piece-time">
                <b>第 {i + 1} 段</b>
                <input className="inp" type="number" step={0.1} defaultValue={+a.toFixed(2)} onBlur={(e) => saveTime(i, 0, Number(e.target.value))} />→
                <input className="inp" type="number" step={0.1} defaultValue={+b.toFixed(2)} onBlur={(e) => saveTime(i, 1, Number(e.target.value))} />
                <span className="dim">{(b - a).toFixed(1)}s</span>
              </div>
              <label className="lbl">原文</label>
              <textarea className="inp ja" rows={2} value={texts.orig[i]} onChange={(e) => setPiece("orig", i, e.target.value)} onBlur={() => saveText("orig")} />
              {d.targets.map((t) => (
                <div key={t}>
                  <label className="lbl">{langName(t, s.langs)}</label>
                  <textarea className="inp" rows={2} style={{ fontSize: 15 }} value={texts[t][i]} onChange={(e) => setPiece(t, i, e.target.value)} onBlur={() => saveText(t)} />
                </div>
              ))}
            </div>
          </div>
        ))}
        <div className="row" style={{ marginTop: 8 }}>
          <button className="btn sm" disabled={busy} onClick={retranslate} title={multi ? "整句一起重译，候选会按同样的段数分好" : "重新翻译这一句"}>↻ 整句重新翻译</button>
          <input className="inp" style={{ flex: 1 }} placeholder="附加要求，例如：更口语一点" value={instr} onChange={(e) => setInstr(e.target.value)} />
        </div>
        {alts && (
          <div className="alts">
            {alts.length === 0 ? <div className="dim" style={{ marginTop: 8 }}>请求中…</div> : alts.map((alt) => {
              // 分段变了（原文的“｜”位置不同）：原文一起换，每段时间按来源重新算
              const resplit = alt.orig !== cue.orig.text;
              const op = splitBars(alt.orig), tp = splitBars(alt.tr);
              return (
                <button key={alt.orig + alt.tr} onClick={() => {
                  patch({ tr: { [d.targets[0]]: alt.tr }, ...(resplit ? { orig: alt.orig, parts: null } : {}) }, resplit ? "已替换译文并重新分段" : "已替换译文");
                  setAlts(null);
                }}>
                  <div className="dim" style={{ fontSize: 11 }}>{tp.length > 1 ? `分 ${tp.length} 段` : "不分段"}{resplit ? " · 分段有调整" : ""}</div>
                  {tp.map((p, i) => <div key={i} className="piece">{p}{resplit && op[i] && <div className="dim ja" style={{ fontSize: 11 }}>{op[i]}</div>}</div>)}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {words.length > 0 && (
        <div className="isec">
          <h3>读音 <span className="dim" style={{ fontWeight: 400 }}>点汉字改读音</span></h3>
          <div className="origedit">
            {words.map((w, wi) => w.segs.map((sg, si) => (sg.length > 1
              ? <ruby key={`${wi}-${si}`} className={w.guess ? "guess" : ""} title={w.guess ? "读音来自分词器，待确认" : ""}
                      onClick={(e) => { const r = e.currentTarget.getBoundingClientRect(); setPop({ wi, si, x: r.left, y: r.bottom + 6, value: sg[1]! }); }}>
                  {sg[0]}<rt>{sg[1]}</rt></ruby>
              : <span key={`${wi}-${si}`}>{sg[0]}</span>)))}
          </div>
        </div>
      )}

      <div className="isec">
        <h3>来源对照 <span className="dim" style={{ fontWeight: 400 }}>{cue.sources.length} 路</span></h3>
        {cue.sources.map((id) => {
          const it = items[id];
          if (!it) return null;
          return (
            <div key={id} className="srcitem">
              <span className={`badge b-${id[0]}`}>{SRC_LBL[id[0]]}</span><div className="tx">{it.text}</div>
              <div className="meta">{id} · {fmt(it.start)}–{fmt(it.end)}{it.ocr_score ? ` · 置信度 ${Math.round(it.ocr_score * 100)}%` : ""}</div>
              {it.asr_hint && <div className="meta" style={{ color: "var(--warn)" }}>语音参考：{it.asr_hint}</div>}
              {it.crop && s.pid && <img src={api.cropUrl(s.pid, it.crop)} alt={id} />}
            </div>
          );
        })}
      </div>

      <div className="row wrap">
        <button className="btn pri" disabled={busy} onClick={() => patch({ review: cue.review === "ok" ? "todo" : "ok" }, cue.review === "ok" ? "已改回待审校" : "已标记为已审")}>
          {cue.review === "ok" ? "✓ 已审（点击撤销）" : "✓ 标记已审"}</button>
        <button className="btn" disabled={busy || !canSplit} title={canSplit ? `在 ${fmt(time)} 处拆成两条字幕` : "把播放头移到这条字幕中间"} onClick={onSplit}>✂ 在播放头拆成两条</button>
        <button className="btn" disabled={busy || !prev} title={prev ? mergeTip(prev, cue) : "上面没有字幕"} onClick={onMergeUp}
                onMouseEnter={() => prev && onHint(prev.id)} onMouseLeave={() => onHint(null)}>↑ 与上一条 #{index} 合并</button>
        <button className="btn" disabled={busy || !next} title={next ? mergeTip(cue, next) : "下面没有字幕"} onClick={onMergeDown}
                onMouseEnter={() => next && onHint(next.id)} onMouseLeave={() => onHint(null)}>↓ 与下一条 #{index + 2} 合并</button>
        <button className="btn ghost" disabled={busy} onClick={() => patch({ delete: true }, "已删除（Ctrl+Z 可撤销）")}>删除</button>
      </div>

      {pop && (
        <div className="pop" style={{ left: Math.min(window.innerWidth - 260, pop.x), top: pop.y }}>
          <div className="lbl" style={{ marginBottom: 6 }}>「{words[pop.wi].segs[pop.si][0]}」的读音（记入读音词典：{words[pop.wi].t}）</div>
          <input className="inp ja" autoFocus value={pop.value} onChange={(e) => setPop({ ...pop, value: e.target.value })}
                 onKeyDown={(e) => { if (e.key === "Enter") saveReading(); if (e.key === "Escape") setPop(null); }} />
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
            <button className="btn sm" onClick={() => setPop(null)}>取消</button><button className="btn sm pri" onClick={saveReading}>保存</button>
          </div>
        </div>
      )}
    </>
  );
}
