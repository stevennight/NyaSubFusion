import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Player, type PlayerHandle } from "../components/Player";
import { usePreview } from "../hooks";
import { useStore } from "../store";
import type { Annotate, Layer, Profile, Style } from "../types";
import { fmt, langName, setIn } from "../util";

const ROLE_LBL = { original: "原文", translation: "译文", transliteration: "整行转写" } as const;
const ANN_LBL: Record<string, string> = { none: "无", furigana: "振假名", pinyin: "拼音", zhuyin: "注音符号" };
const FONTS = ["Microsoft YaHei", "Yu Gothic", "Meiryo", "Yu Mincho", "SimHei", "KaiTi", "Segoe UI", "Arial"];
const DEFAULT_ANN: Annotate = { kind: "none", scope: "all", easy_kanji: "grade2", known_words: [], numbers: false, script: "hiragana", tone: "mark", size: 0.5, color: "", srt: "paren" };
const newLayer = (role: Layer["role"], style: string): Layer => ({ role, lang: "", of: "original", show: "on", style, max_chars: 0, annotate: { ...DEFAULT_ANN }, tone: "mark" });

export function LayersPage() {
  const s = useStore();
  const d = s.data;
  const eff = s.config?.effective;
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [styles, setStyles] = useState<Record<string, Style>>({});
  const [marks, setMarks] = useState("");  // 输出前去掉的符号，空格分隔
  const [pi, setPi] = useState(0);
  const [li, setLi] = useState(0);
  const [state, setState] = useState<"saved" | "pending" | "saving">("saved");
  const loaded = useRef(false);
  const player = useRef<PlayerHandle>(null);
  const [, setTime] = useState(0);
  const onTime = useCallback((t: number) => setTime(t), []);

  useEffect(() => {
    if (!eff || loaded.current) return;
    loaded.current = true;
    setProfiles(eff.render.profiles);
    setStyles(eff.render.styles);
    setMarks(((eff.render.strip_marks as string[] | undefined) ?? ["➡"]).join(" "));
  }, [eff]);

  // 改动 0.5 秒后自动保存：只影响注音 / 输出，预览随之刷新
  const first = useRef(true);
  useEffect(() => {
    if (!loaded.current || !s.config) return;
    if (first.current) { first.current = false; return; }
    setState("pending");
    const t = setTimeout(async () => {
      setState("saving");
      try {
        let project = setIn(s.config!.project, "render.profiles", profiles);
        project = setIn(project, "render.styles", styles);
        project = setIn(project, "render.strip_marks", marks.split(/\s+/).filter(Boolean));
        await s.setConfig(project);
        setState("saved");
      } catch (e) { s.setError((e as Error).message); setState("pending"); }
    }, 500);
    return () => clearTimeout(t);
  }, [profiles, styles, marks]);

  const prof = profiles[pi];
  const preview = usePreview(prof?.name);
  // 一打开就跳到第一条有字幕的地方（开头常是黑场，看不出样式）
  const firstT = d ? (d.cues[0]?.start ?? d.subs[0]?.start ?? d.ocr[0]?.start) : undefined;
  const jumped = useRef(false);
  useEffect(() => {
    if (jumped.current || firstT === undefined || !prof) return;
    jumped.current = true;
    const t = setTimeout(() => player.current?.seek(firstT + 0.5), 400);
    return () => clearTimeout(t);
  }, [firstT, prof]);
  if (!d || !eff || !prof) return <div className="empty">加载中…</div>;
  const layer = prof.layers[li] ?? prof.layers[0];

  const updProf = (patch: Partial<Profile>) => setProfiles((ps) => ps.map((p, i) => (i === pi ? { ...p, ...patch } : p)));
  const updLayers = (layers: Layer[]) => updProf({ layers });
  const updLayer = (patch: Partial<Layer>) => updLayers(prof.layers.map((l, i) => (i === li ? { ...l, ...patch } : l)));
  const updAnn = (patch: Partial<Annotate>) => updLayer({ annotate: { ...layer.annotate, ...patch } });
  const layerLang = (l: Layer) => (l.role === "original" ? d.src : l.role === "translation" ? l.lang || d.targets[0] : l.of === "original" ? d.src : l.of);
  const layerName = (l: Layer) => `${ROLE_LBL[l.role]} · ${langName(layerLang(l), s.langs)}`;
  const pack = (code: string) => s.langs.find((x) => x.code === code);
  const move = (i: number, dir: number) => {
    const j = i + dir;
    if (j < 0 || j >= prof.layers.length) return;
    const ls = [...prof.layers];
    [ls[i], ls[j]] = [ls[j], ls[i]];
    updLayers(ls);
    setLi(j);
  };
  const st = styles[layer?.style] ?? styles.main;
  const updStyle = (patch: Partial<Style>) => setStyles((ss) => ({ ...ss, [layer.style]: { ...st, ...patch } }));
  // “跳到”：有字幕就挑几条字幕；还没运行过就用识别到的来源（自带字幕 / 画面字），都没有就均匀取几个时间点
  const pick = <T,>(xs: T[]) => xs.filter((_, i) => i % Math.max(1, Math.floor(xs.length / 8)) === 0).slice(0, 8);
  const dur = s.summary?.meta?.duration ?? 0;
  const samples: { t: number; label: string }[] = d.cues.length
    ? pick(d.cues).map((c) => ({ t: c.start + 0.3, label: c.orig.text.slice(0, 8) }))
    : (d.subs.length || d.ocr.length)
      ? pick(d.subs.length ? d.subs : d.ocr).map((c) => ({ t: c.start + 0.3, label: c.text.slice(0, 8) }))
      : dur > 0 ? Array.from({ length: 8 }, (_, i) => ({ t: (dur * (i + 0.5)) / 8, label: "" })) : [];

  return (
    <div className="layers">
      <div className="lyside">
        <div className="card">
          <div className="row" style={{ marginBottom: 8 }}>
            <h3 style={{ margin: 0 }}>输出方案</h3><span className="sp" />
            <span className={`chip ${state === "saved" ? "ok" : "warn"}`}>{state === "saved" ? "已应用" : state === "saving" ? "应用中…" : "待应用"}</span>
            <button className="btn sm" onClick={() => { setProfiles([...profiles, { ...prof, name: `${prof.name}-副本` }]); setPi(profiles.length); }}>复制</button>
            <button className="btn sm" disabled={profiles.length <= 1} onClick={() => { setProfiles(profiles.filter((_, i) => i !== pi)); setPi(0); setLi(0); }}>删除</button>
          </div>
          <select className="inp" value={pi} onChange={(e) => { setPi(Number(e.target.value)); setLi(0); }}>
            {profiles.map((p, i) => <option key={i} value={i}>{p.name}（{p.format.toUpperCase()}）{p.enabled ? "" : " · 停用"}</option>)}
          </select>
          <div className="kv" style={{ marginTop: 10 }}>
            <span className="lbl">名字</span><input className="inp" value={prof.name} onChange={(e) => updProf({ name: e.target.value })} />
            <span className="lbl">格式</span>
            <div className="row"><div className="seg">{(["ass", "srt"] as const).map((f) => <button key={f} className={prof.format === f ? "on" : ""} onClick={() => updProf({ format: f })}>{f.toUpperCase()}</button>)}</div>
              <label className="row dim"><input type="checkbox" checked={prof.enabled} onChange={(e) => updProf({ enabled: e.target.checked })} />导出时启用</label></div>
            <span className="lbl">文件名</span><input className="inp" value={prof.filename} onChange={(e) => updProf({ filename: e.target.value })} />
            <span className="lbl">说明文字</span>
            <div className="seg">{([["near", "贴原文旁"], ["top", "顶部"], ["off", "不显示"]] as const).map(([v, l]) => <button key={v} className={prof.info_mode === v ? "on" : ""} onClick={() => updProf({ info_mode: v })}>{l}</button>)}</div>
            <span className="lbl">排版</span>
            <div className="row"><label className="row dim"><input type="checkbox" checked={prof.avoid_hardsub} onChange={(e) => updProf({ avoid_hardsub: e.target.checked })} />避开硬字幕</label>
              最多<div className="seg">{[1, 2, 3].map((n) => <button key={n} className={prof.max_lines === n ? "on" : ""} onClick={() => updProf({ max_lines: n })}>{n}</button>)}</div>行</div>
            <span className="lbl">去掉的符号</span>
            <input className="inp" value={marks} placeholder="例如 ➡" title="原文、译文里出现这些符号时去掉（所有方案通用，空格分隔）。默认去掉日文字幕行尾表示“接下一条”的 ➡"
                   onChange={(e) => setMarks(e.target.value)} />
          </div>
        </div>

        <div className="card">
          <div className="row" style={{ marginBottom: 8 }}><h3 style={{ margin: 0 }}>图层</h3><span className="dim" style={{ fontSize: 12 }}>上 → 下 即画面中从上到下</span></div>
          {prof.layers.map((l, i) => (
            <div key={i} className={`lycard ${i === li ? "sel" : ""}`} onClick={() => setLi(i)}>
              <div className="ord"><button onClick={(e) => { e.stopPropagation(); move(i, -1); }}>▲</button><button onClick={(e) => { e.stopPropagation(); move(i, 1); }}>▼</button></div>
              <div className="nm"><b>{layerName(l)}</b><small>{l.style} · {styles[l.style]?.size ?? "?"}{l.annotate.kind !== "none" ? ` · ${ANN_LBL[l.annotate.kind]}` : ""}{l.show === "auto" ? " · 画面有原文时隐藏" : ""}</small></div>
              <div className="seg" onClick={(e) => e.stopPropagation()}>
                {(l.role === "original" ? (["on", "auto", "off"] as const) : (["on", "off"] as const)).map((v) =>
                  <button key={v} className={l.show === v ? "on" : ""} onClick={() => updLayers(prof.layers.map((x, k) => (k === i ? { ...x, show: v } : x)))}>{{ on: "开", auto: "自动", off: "关" }[v]}</button>)}
              </div>
              <button className="del" title="删除图层" onClick={(e) => { e.stopPropagation(); updLayers(prof.layers.filter((_, k) => k !== i)); setLi(0); }}>×</button>
            </div>
          ))}
          <div className="row wrap">
            <button className="btn sm" onClick={() => { updLayers([...prof.layers, newLayer("translation", "sub")]); setLi(prof.layers.length); }}>＋ 译文层</button>
            <button className="btn sm" onClick={() => { updLayers([...prof.layers, newLayer("original", "sub")]); setLi(prof.layers.length); }}>＋ 原文层</button>
            <button className="btn sm" onClick={() => { updLayers([...prof.layers, newLayer("transliteration", "translit")]); setLi(prof.layers.length); }}>＋ 转写层</button>
          </div>
        </div>

        {layer && (
          <div className="card">
            <h3>「{layerName(layer)}」</h3>
            <div className="kv">
              {layer.role === "translation" && (<><span className="lbl">语言</span>
                <select className="inp" value={layer.lang || d.targets[0]} onChange={(e) => {
                  const p = pack(e.target.value);
                  updLayer({ lang: e.target.value, annotate: p?.annotations.includes(layer.annotate.kind) ? layer.annotate : { ...layer.annotate, kind: "none" } });
                }}>{d.targets.map((t) => <option key={t} value={t}>{langName(t, s.langs)}</option>)}</select></>)}
              {layer.role === "original" && (<><span className="lbl">语言</span><span><span className="chip">{langName(d.src, s.langs)}（源语言）</span></span></>)}
              {layer.role === "transliteration" && (<><span className="lbl">转写对象</span>
                <select className="inp" value={layer.of} onChange={(e) => updLayer({ of: e.target.value })}>
                  {[["original", d.src], ...d.targets.map((t) => [t, t])].filter(([, c]) => pack(c)?.translit).map(([v, c]) =>
                    <option key={v} value={v}>{v === "original" ? "原文" : "译文"}（{langName(c, s.langs)}）→ {pack(c)?.translit}</option>)}
                </select>
                {pack(layerLang(layer))?.code.startsWith("zh") && (<><span className="lbl">声调</span>
                  <div className="seg">{([["mark", "pīn"], ["number", "pin1"], ["none", "pin"]] as const).map(([v, l]) => <button key={v} className={layer.tone === v ? "on" : ""} onClick={() => updLayer({ tone: v })}>{l}</button>)}</div></>)}</>)}
              <span className="lbl">样式</span>
              <div className="row">
                <select className="inp" style={{ flex: 1 }} value={layer.style} onChange={(e) => updLayer({ style: e.target.value })}>
                  {Object.keys(styles).map((n) => <option key={n}>{n}</option>)}</select>
                <button className="btn sm" onClick={() => { const n = prompt("新样式名（英文）"); if (n && !styles[n]) { setStyles({ ...styles, [n]: { ...st } }); updLayer({ style: n }); } }}>＋</button>
              </div>
              {layer.role !== "transliteration" && (<><span className="lbl">每行字数</span>
                <div className="row"><input type="range" min={0} max={60} value={layer.max_chars} onChange={(e) => updLayer({ max_chars: Number(e.target.value) })} /><span className="dim" style={{ width: 70 }}>{layer.max_chars || "语言默认"}</span></div></>)}
            </div>

            <h3 style={{ marginTop: 14 }}>样式「{layer.style}」<span className="dim" style={{ fontWeight: 400 }}> 改动会影响所有用这个样式的图层</span></h3>
            <div className="kv">
              <span className="lbl">字体</span>
              <><input className="inp" list="fonts" value={st.font} onChange={(e) => updStyle({ font: e.target.value })} /><datalist id="fonts">{FONTS.map((f) => <option key={f} value={f} />)}</datalist></>
              <span className="lbl">字号</span>
              <div className="row"><input type="range" min={14} max={96} value={st.size} onChange={(e) => updStyle({ size: Number(e.target.value) })} /><span className="dim" style={{ width: 28 }}>{st.size}</span></div>
              <span className="lbl">颜色 / 描边</span>
              <div className="row"><input type="color" value={st.color} onChange={(e) => updStyle({ color: e.target.value })} /><input type="color" value={st.outline_color} onChange={(e) => updStyle({ outline_color: e.target.value })} />
                <input type="range" min={0} max={8} step={0.2} value={st.outline} onChange={(e) => updStyle({ outline: Number(e.target.value) })} />
                <label className="row dim"><input type="checkbox" checked={st.bold} onChange={(e) => updStyle({ bold: e.target.checked })} />粗</label>
                <label className="row dim"><input type="checkbox" checked={st.italic} onChange={(e) => updStyle({ italic: e.target.checked })} />斜</label></div>
            </div>

            {layer.role !== "transliteration" && (() => {
              const p = pack(layerLang(layer));
              const a = layer.annotate;
              return (<>
                <h3 style={{ marginTop: 14 }}>注音 <span className="dim" style={{ fontWeight: 400 }}>— 由「{p?.name ?? layerLang(layer)}」语言包提供</span></h3>
                {!p?.annotations.length ? <div className="dim" style={{ fontSize: 12 }}>{p?.name ?? "这门语言"}没有可用的注音。</div> : (
                  <div className="kv">
                    <span className="lbl">类型</span>
                    <div className="seg">{["none", ...p.annotations].map((k) => <button key={k} className={a.kind === k ? "on" : ""} onClick={() => updAnn({ kind: k as Annotate["kind"] })}>{ANN_LBL[k]}</button>)}</div>
                    {a.kind === "furigana" && (<>
                      <span className="lbl">范围</span>
                      <div className="seg">{([["all", "全部"], ["difficult", "难读"], ["proper", "专有名词"], ["first", "首次出现"]] as const).map(([v, l]) => <button key={v} className={a.scope === v ? "on" : ""} onClick={() => updAnn({ scope: v })}>{l}</button>)}</div>
                      {a.scope === "difficult" && (<><span className="lbl">简单汉字</span>
                        <div className="seg">{([["grade1", "小学一年级"], ["grade2", "一、二年级"]] as const).map(([v, l]) => <button key={v} className={a.easy_kanji === v ? "on" : ""} onClick={() => updAnn({ easy_kanji: v })}>{l}</button>)}</div>
                        <span className="lbl">已会的词</span><input className="inp ja" placeholder="用空格分隔，这些词不注音" value={a.known_words.join(" ")} onChange={(e) => updAnn({ known_words: e.target.value.split(/\s+/).filter(Boolean) })} /></>)}
                      <span className="lbl">文字</span>
                      <div className="seg">{([["hiragana", "ひらがな"], ["katakana", "カタカナ"], ["romaji", "romaji"]] as const).map(([v, l]) => <button key={v} className={a.script === v ? "on" : ""} onClick={() => updAnn({ script: v })}>{l}</button>)}</div>
                      <span className="lbl">数字</span><label className="row dim"><input type="checkbox" checked={a.numbers} onChange={(e) => updAnn({ numbers: e.target.checked })} />数字也注音（读音易错）</label>
                    </>)}
                    {a.kind === "pinyin" && (<><span className="lbl">声调</span>
                      <div className="seg">{([["mark", "pīn yīn"], ["number", "pin1 yin1"], ["none", "pin yin"]] as const).map(([v, l]) => <button key={v} className={a.tone === v ? "on" : ""} onClick={() => updAnn({ tone: v })}>{l}</button>)}</div></>)}
                    {a.kind !== "none" && (<>
                      <span className="lbl">大小 / 颜色</span>
                      <div className="row"><input type="range" min={0.3} max={0.7} step={0.02} value={a.size} onChange={(e) => updAnn({ size: Number(e.target.value) })} />
                        <input type="color" value={a.color || st.color} onChange={(e) => updAnn({ color: e.target.value })} />
                        {a.color && <button className="btn sm ghost" onClick={() => updAnn({ color: "" })}>同底字</button>}</div>
                      <span className="lbl">SRT</span>
                      <div className="seg">{([["paren", "漢字(かんじ)"], ["none", "不输出"]] as const).map(([v, l]) => <button key={v} className={a.srt === v ? "on" : ""} onClick={() => updAnn({ srt: v })}>{l}</button>)}</div>
                    </>)}
                  </div>
                )}
              </>);
            })()}
          </div>
        )}
      </div>

      <div className="lyprev">
        <div className="lyplayer">
          <Player ref={player} preview={preview} onTime={onTime} controls />
        </div>
        {samples.length > 0 && (
          <div className="row wrap">
            <span className="dim">跳到：</span>
            {samples.map((c, i) => <button key={i} className="btn sm" onClick={() => player.current?.seek(c.t)}>{fmt(c.t).slice(0, 5)}{c.label ? ` ${c.label}` : ""}</button>)}
          </div>
        )}
        <div className="row">
          <button className="btn pri" onClick={async () => {
            if (!s.pid) return;
            try { const r = await api.export(s.pid, [prof.name]); s.toast(`已导出：${r.files.join("、")}`); } catch (e) { s.setError((e as Error).message); }
          }}>导出这个方案</button>
          <button className="btn" disabled={s.running || !s.data?.cues.length} onClick={() => s.exportVideo("mux", [prof.name])}>封装进 MKV</button>
          <button className="btn" disabled={s.running || !s.data?.cues.length || prof.format !== "ass"} title={prof.format !== "ass" ? "压制需要 ASS 方案" : ""}
                  onClick={() => s.exportVideo("burn", [prof.name])}>压制硬字幕</button>
          <span className="dim" style={{ fontSize: 12 }}>预览和导出的 ASS 用同一套排版位置；带注音的 ASS 播放端需要有同一字体。</span>
        </div>
      </div>
    </div>
  );
}
