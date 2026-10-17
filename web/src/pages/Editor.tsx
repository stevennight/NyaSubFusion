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
                target={target} peaks={peaks} selected={sel} onSeek={(t) => player.current?.seek(t)} onSelect={(id) => select(id)}
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
                <tr key={c.id} className={`${c.id === sel ? "sel" : ""} ${current.has(c.id) ? "cur" : ""}`} onClick={() => select(c.id)}>
                  <td><span className={`dot ${c.review}`} /></td><td className="dim">{cues.indexOf(c) + 1}</td>
                  <td className="t">{fmt(c.start)} → {fmt(c.end)}</td><td className="typ">{TYPE_LBL[c.type] ?? c.type}</td>
                  <td>{c.sources.map((x) => <span key={x} className={`badge b-${x[0]}`} style={{ marginRight: 3 }}>{SRC_LBL[x[0]]}</span>)}</td>
                  <td className="ja">{c.orig.text}</td><td>{c.tr[target]?.text}</td>
                  <td className="fl">{c.flags.map((f) => FLAG_LBL[f] ?? f).join(" · ")}</td>
                </tr>
              ))}
            </tbody></table>
        </div>
      </div>
      <aside className="inspector">
        {selected ? <Inspector key={selected.id + JSON.stringify(selected.tr) + selected.orig.text} cue={selected} index={cues.indexOf(selected)} onStep={step}
                               time={time} isLast={cues.indexOf(selected) === cues.length - 1}
                               onSplit={() => edit(() => api.splitCue(s.pid!, selected.id, time), "已拆成两条")}
                               onMerge={() => edit(() => api.mergeNext(s.pid!, selected.id), "已和下一条合并")} />
          : <div className="isec dim">点时间轴或列表里的一条字幕，在这里对照来源、修改原文 / 译文 / 读音。</div>}
      </aside>
    </div>
  );
}

function Inspector({ cue, index, onStep, time, isLast, onSplit, onMerge }: {
  cue: Cue; index: number; onStep: (d: number) => void; time: number; isLast: boolean; onSplit: () => void; onMerge: () => void;
}) {
  const canSplit = time > cue.start + 0.2 && time < cue.end - 0.2;
  const s = useStore();
  const d = s.data!;
  const [texts, setTexts] = useState<Record<string, string>>(() => ({ orig: cue.orig.text, ...Object.fromEntries(d.targets.map((t) => [t, cue.tr[t]?.text ?? ""])) }));
  const [alts, setAlts] = useState<string[] | null>(null);
  const [instr, setInstr] = useState("");
  const [busy, setBusy] = useState(false);
  const [pop, setPop] = useState<{ wi: number; si: number; x: number; y: number; value: string } | null>(null);
  const items = Object.fromEntries(d.fused.items.map((it) => [it.id, it]));
  const words: Word[] = d.tokens[cue.id]?.orig ?? [];

  const patch = async (body: Record<string, unknown>, msg?: string) => {
    if (!s.pid) return;
    setBusy(true);
    try { await api.editCue(s.pid, cue.id, body); await s.reload(); if (msg) s.toast(msg); }
    catch (e) { s.setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const saveText = (key: string) => {
    const old = key === "orig" ? cue.orig.text : cue.tr[key]?.text ?? "";
    if (texts[key] === old) return;
    patch(key === "orig" ? { orig: texts[key] } : { tr: { [key]: texts[key] } }, "已保存");
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
          <div className="row">
            <input className="inp" type="number" step={0.1} defaultValue={cue.start} style={{ width: 84 }} onBlur={(e) => Number(e.target.value) !== cue.start && patch({ start: Number(e.target.value) })} />→
            <input className="inp" type="number" step={0.1} defaultValue={cue.end} style={{ width: 84 }} onBlur={(e) => Number(e.target.value) !== cue.end && patch({ end: Number(e.target.value) })} />
            <span className="dim">{(cue.end - cue.start).toFixed(1)}s</span>
          </div>
          <span className="lbl">类型</span>
          <select className="inp" value={cue.type} onChange={(e) => patch({ type: e.target.value })}>
            {Object.entries(TYPE_LBL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        {cue.flags.length > 0 && <div className="row wrap" style={{ marginTop: 8 }}>{cue.flags.map((f) => <span key={f} className="chip warn">{FLAG_LBL[f] ?? f}</span>)}</div>}
      </div>

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

      <div className="isec">
        <h3>原文 <span className="dim" style={{ fontWeight: 400 }}>点汉字改读音</span></h3>
        {words.length > 0 && (
          <div className="origedit">
            {words.map((w, wi) => w.segs.map((sg, si) => (sg.length > 1
              ? <ruby key={`${wi}-${si}`} className={w.guess ? "guess" : ""} title={w.guess ? "读音来自分词器，待确认" : ""}
                      onClick={(e) => { const r = e.currentTarget.getBoundingClientRect(); setPop({ wi, si, x: r.left, y: r.bottom + 6, value: sg[1]! }); }}>
                  {sg[0]}<rt>{sg[1]}</rt></ruby>
              : <span key={`${wi}-${si}`}>{sg[0]}</span>)))}
          </div>
        )}
        <textarea className="inp ja" rows={2} style={{ marginTop: 6 }} value={texts.orig} onChange={(e) => setTexts({ ...texts, orig: e.target.value })} onBlur={() => saveText("orig")} />
      </div>

      <div className="isec">
        <div className="row" style={{ marginBottom: 6 }}><h3 style={{ margin: 0 }}>译文</h3><span className="sp" />
          <button className="btn sm" disabled={busy} onClick={retranslate}>↻ 重新翻译</button></div>
        {d.targets.map((t) => (
          <div key={t} className="field">
            <label>{langName(t, s.langs)}</label>
            <textarea className="inp" rows={2} style={{ fontSize: 15 }} value={texts[t]} onChange={(e) => setTexts({ ...texts, [t]: e.target.value })} onBlur={() => saveText(t)} />
          </div>
        ))}
        <input className="inp" placeholder="重译的附加要求，例如：更口语一点" value={instr} onChange={(e) => setInstr(e.target.value)} />
        {alts && (
          <div className="alts">
            {alts.length === 0 ? <div className="dim" style={{ marginTop: 8 }}>请求中…</div> : alts.map((a) => (
              <button key={a} onClick={() => { setTexts({ ...texts, [d.targets[0]]: a }); patch({ tr: { [d.targets[0]]: a } }, "已替换译文"); setAlts(null); }}>{a}</button>
            ))}
          </div>
        )}
      </div>

      <div className="row wrap">
        <button className="btn pri" disabled={busy} onClick={() => patch({ review: cue.review === "ok" ? "todo" : "ok" }, cue.review === "ok" ? "已改回待审校" : "已标记为已审")}>
          {cue.review === "ok" ? "✓ 已审（点击撤销）" : "✓ 标记已审"}</button>
        <button className="btn" disabled={busy || !canSplit} title={canSplit ? `在 ${fmt(time)} 处拆开` : "把播放头移到这条字幕中间"} onClick={onSplit}>✂ 在播放头拆分</button>
        <button className="btn" disabled={busy || isLast} onClick={onMerge}>与下一条合并</button>
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
