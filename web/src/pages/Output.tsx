// 输出：左边一份清单（字幕文件 · 视频 · 样式 · 通用），右边编辑 + 预览。改了自动保存。
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Modal } from "../components/Modal";
import { lastPos, Player, type PlayerHandle } from "../components/Player";
import { SaveChip, templateChange, useDoc, useDraft, useUnsaved, type ConfigDoc } from "../doc";
import { usePreview } from "../hooks";
import { useStore } from "../store";
import type { Annotate, Cfg, LangInfo, Layer, PreviewHl, Profile, Style } from "../types";
import { fmt, langName, pct, setIn, STAGE_LBL } from "../util";

type Video = {
  mode: "off" | "mux" | "burn"; profiles: string[]; keep_subs: boolean; fonts: string; encoder: string;
  quality: number; height: number; broadcast: boolean; default: string; burn: string;
};
interface R {
  profiles: Profile[]; styles: Record<string, Style>; strip_marks: string[]; video: Video;
  broadcast: boolean; broadcast_font: string; fallback_style: string;
}
type Upd = (f: (r: R) => R) => void;

const KEYS: (keyof R)[] = ["profiles", "styles", "strip_marks", "video", "broadcast", "broadcast_font", "fallback_style"];
const load = (doc: ConfigDoc): R => {
  const r = structuredClone(doc.effective.render ?? {});
  return { ...r, strip_marks: r.strip_marks ?? ["➡"], fallback_style: r.fallback_style ?? "main", broadcast_font: r.broadcast_font ?? "" } as R;
};
const write = (raw: Cfg, r: R) => KEYS.reduce((out, k) => setIn(out, `render.${k}`, r[k]), raw);

const ROLE_LBL = { original: "原文", translation: "译文", transliteration: "整行转写" } as const;
const ANN_LBL: Record<string, string> = { none: "无", furigana: "振假名", pinyin: "拼音", zhuyin: "注音符号" };
const FONTS = ["Microsoft YaHei", "Yu Gothic", "Meiryo", "Yu Mincho", "BIZ UDGothic", "SimHei", "KaiTi", "Segoe UI", "Arial"];
const FONTS_LBL: Record<string, string> = { subset: "嵌入字体（只含用到的字）", full: "嵌入完整字体", none: "不嵌入字体" };
const ENCODERS: [string, string][] = [["auto", "自动（和原片同一种编码，先试显卡）"], ["h264_nvenc", "H.264 · NVIDIA 显卡"], ["libx264", "H.264 · CPU"],
  ["hevc_nvenc", "H.265 · NVIDIA 显卡"], ["libx265", "H.265 · CPU"]];
const DEFAULT_ANN: Annotate = { kind: "none", scope: "all", easy_kanji: "grade2", known_words: [], numbers: false, script: "hiragana", tone: "mark", size: 0.5, color: "", srt: "paren" };
// 新加的原文层和出厂的一样默认“自动”：画面上已经有这句硬字幕时不重复显示
const newLayer = (role: Layer["role"], style: string): Layer => ({ role, lang: "", of: "original", show: role === "original" ? "auto" : "on", style, max_chars: 0, annotate: { ...DEFAULT_ANN }, tone: "mark" });
const FACTORY_LBL: Record<string, string> = { main: "正文", sub: "小字", orig_main: "原文大字", info: "说明", translit: "转写" };

/** 一个样式用在哪：图层 + 各 ASS 字幕文件的说明文字 */
function usedBy(r: R, name: string) {
  const out: { label: string; sel: string }[] = [];
  r.profiles.forEach((p, i) => {
    p.layers.forEach((l) => { if (l.style === name) out.push({ label: `${p.name} › ${ROLE_LBL[l.role]}层`, sel: `f:${i}` }); });
    if (p.format === "ass" && p.info_mode !== "off" && (p.info_style || "info") === name) out.push({ label: `${p.name} › 说明文字`, sel: `f:${i}` });
  });
  return out;
}

export function OutputPage() {
  const s = useStore();
  const doc = useDoc();
  const { v: r, update, state } = useDraft(doc, load, write);
  const [sel, setSel] = useState("f:0");
  const [li, setLi] = useState(0);   // 字幕文件里展开的图层（预览里框出来）
  if (!doc || !r) return <div className="empty">加载中…</div>;
  const [kind, key] = [sel.slice(0, 1), sel.slice(2)];
  const episode = doc.kind === "episode";
  const bc = episode ? s.summary?.broadcast : null;
  const subsRead = s.summary?.broadcast_check !== "checking";   // 已经知道有没有广播字幕（没运行也会预先检查）
  const fileIdx = kind === "f" && key !== "broadcast" ? Number(key) : -1;
  const prof = r.profiles[fileIdx];
  // 预览哪个字幕文件：选中的；选了样式就找用到它的；否则第一个输出的 ASS
  const firstAss = r.profiles.find((p) => p.enabled && p.format === "ass") ?? r.profiles[0];
  const previewName = prof?.name ?? (kind === "s" ? r.profiles[Number(usedBy(r, key)[0]?.sel.slice(2))]?.name : undefined)
    ?? (kind === "v" && key === "burn" && r.video.burn && r.video.burn !== "broadcast" ? r.video.burn : firstAss?.name);
  const ctx = { r, update, setSel, doc };

  return (
    <div className="page outpage"><div className="wide">
      <div className="out">
        <OutList {...ctx} sel={sel} bc={bc ?? null} subsRead={subsRead} state={state} />
        <div className="oed">
          <div className="col">
            {kind === "f" && prof && <FileEditor {...ctx} i={fileIdx} li={li} setLi={setLi} />}
            {kind === "f" && key === "broadcast" && <BroadcastEditor {...ctx} bc={bc ?? null} subsRead={subsRead} />}
            {kind === "v" && <VideoEditor {...ctx} mode={key as "mux" | "burn"} bc={bc ?? null} />}
            {kind === "s" && r.styles[key] && <StyleEditor {...ctx} name={key} />}
            {kind === "g" && <CleanEditor {...ctx} />}
            {((kind === "s" && !r.styles[key]) || (kind === "f" && key !== "broadcast" && !prof)) && <div className="card dim">在左边选一项</div>}
          </div>
          {episode ? <OutPreview profile={previewName} exportable={kind === "f" ? prof?.name : undefined}
                                 hl={kind === "f" && prof?.layers[li] ? { layer: li } : kind === "s" ? { style: key } : undefined}
                                 hlLabel={kind === "f" && prof?.layers[li] ? `${ROLE_LBL[prof.layers[li].role]}层` : kind === "s" ? `样式「${key}」` : ""} />
            : <div className="card dim" style={{ fontSize: 12.5 }}>预览要用一集的画面和字幕，打开一集时在「输出」里看。</div>}
        </div>
      </div>
    </div></div>
  );
}

function Sw({ on, onChange, disabled, title }: { on: boolean; onChange: (v: boolean) => void; disabled?: boolean; title?: string }) {
  return <button className={`sw${on ? " on" : ""}`} disabled={disabled} title={title} onClick={(e) => { e.stopPropagation(); onChange(!on); }} />;
}

function Swatch({ st }: { st?: Style }) {
  return <span className="swatch" style={{ color: st?.color, textShadow: `0 0 2px ${st?.outline_color}`, background: st?.box ? st.box_color : undefined }}>字</span>;
}

// ---------------- 清单 ----------------
function OutList({ r, update, setSel, doc, sel, bc, subsRead, state }: {
  r: R; update: Upd; setSel: (s: string) => void; doc: ConfigDoc; sel: string;
  bc: { file: string | null } | null; subsRead: boolean; state: "saved" | "pending" | "saving";
}) {
  // “＋ 新样式”菜单：浮在页面上（列表本身会滚动，放在里面会被裁掉、得往下滚才看得到），下面放不下就往上开
  const [menu, setMenuAt] = useState<React.CSSProperties | null>(null);
  const setMenu = (on: boolean, el?: HTMLElement) => {
    if (!on || !el) return setMenuAt(null);
    const rc = el.getBoundingClientRect();
    const below = window.innerHeight - rc.bottom - 12, above = rc.top - 12;
    setMenuAt({ position: "fixed", left: rc.left, right: "auto", width: 250, overflow: "auto",
      ...(below >= 360 || below >= above ? { top: rc.bottom + 4, bottom: "auto", maxHeight: below } : { top: "auto", bottom: window.innerHeight - rc.top + 4, maxHeight: above }) });
  };
  useEffect(() => {
    if (!menu) return;
    const close = (e: Event) => { if (!(e.target instanceof Element && e.target.closest(".menu"))) setMenuAt(null); };
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => { window.removeEventListener("scroll", close, true); window.removeEventListener("resize", close); };
  }, [menu]);
  const [importing, setImporting] = useState(false);
  const names = Object.keys(r.styles);
  const inUse = names.filter((n) => usedBy(r, n).length || n === r.fallback_style);
  const unused = names.filter((n) => !inUse.includes(n));
  const curStyle = sel.startsWith("s:") ? sel.slice(2) : r.fallback_style;
  const addStyle = (name: string, st: Style) => { update((x) => ({ ...x, styles: { ...x.styles, [name]: st } })); setSel(`s:${name}`); setMenu(false); };
  const freeName = (base: string) => { let n = 2; while (r.styles[`${base}${n}`]) n++; return `${base}${n}`; };
  const muxOn = r.video.mode === "mux", burnOn = r.video.mode === "burn";
  return (
    <div className="olist">
      <div className="row" style={{ padding: "2px 6px 6px" }}><span className="grp" style={{ padding: 0 }}>字幕文件</span><span className="sp" /><SaveChip state={state} /></div>
      {r.profiles.map((p, i) => (
        <div key={i} className={`oi${sel === `f:${i}` ? " sel" : ""}`} onClick={() => setSel(`f:${i}`)}>
          <Sw on={p.enabled} title={p.enabled ? "输出（点一下不输出）" : "不输出（点一下输出）"}
              onChange={(v) => update((x) => ({ ...x, profiles: x.profiles.map((q, j) => (j === i ? { ...q, enabled: v } : q)) }))} />
          <div className="t">{p.name} <span className="dim">· {p.format.toUpperCase()}</span><small>{p.layers.map((l) => ROLE_LBL[l.role]).join(" + ") || "没有图层"}</small></div>
        </div>
      ))}
      <div className={`oi${sel === "f:broadcast" ? " sel" : ""}${doc.kind === "episode" && !bc ? " na" : ""}`} onClick={() => setSel("f:broadcast")}>
        <Sw on={r.broadcast && (doc.kind !== "episode" || !!bc)} disabled={doc.kind === "episode" && !bc}
            title={doc.kind === "episode" && !bc ? (subsRead ? "这个视频的字幕不是电视广播字幕（或者没用视频里的字幕）" : "正在检查视频里有没有电视广播字幕") : ""}
            onChange={(v) => update((x) => ({ ...x, broadcast: v }))} />
        <div className="t">广播字幕原样 <span className="dim">· ASS</span><small>{doc.kind === "episode" && !bc ? (subsRead ? "这个视频没有广播字幕" : "正在检查…") : "日语原文，照搬电视上的样子"}</small></div>
      </div>
      <div className="oadd" onClick={() => {
        const base = r.profiles[0];
        const name = (() => { let n = 2; while (r.profiles.some((p) => p.name === `新字幕${n}`)) n++; return `新字幕${n}`; })();
        update((x) => ({ ...x, profiles: [...x.profiles, { ...(base ?? {}), name, enabled: true, format: "ass", filename: "{stem}.{name}.{ext}",
          layers: [newLayer("translation", x.fallback_style)], info_mode: "near", info_style: "info", avoid_hardsub: true, max_lines: 2, min_duration: 1 } as Profile] }));
        setSel(`f:${r.profiles.length}`);
      }}>＋ 新建字幕文件</div>

      <div className="grp">视频</div>
      <div className={`oi${sel === "v:mux" ? " sel" : ""}`} onClick={() => setSel("v:mux")}>
        <span className={`dot ${muxOn ? "done" : "off"}`} /><div className="t">封装 MKV<small>{muxOn ? "运行完自动" : "手动"}</small></div></div>
      <div className={`oi${sel === "v:burn" ? " sel" : ""}`} onClick={() => setSel("v:burn")}>
        <span className={`dot ${burnOn ? "done" : "off"}`} /><div className="t">压制硬字幕<small>{burnOn ? "运行完自动" : "手动"}</small></div></div>

      <div className="grp">样式</div>
      {inUse.map((n) => <StyleItem key={n} r={r} name={n} sel={sel} setSel={setSel} doc={doc} />)}
      {unused.length > 0 && (<>
        <div className="grp" style={{ paddingTop: 4 }}>没在用
          <span className="link" style={{ marginLeft: 8, letterSpacing: 0 }} title="删掉所有没在用的样式（出厂样式以后还能加回来）"
                onClick={() => update((x) => ({ ...x, styles: Object.fromEntries(Object.entries(x.styles).filter(([k]) => !unused.includes(k))) }))}>清理</span></div>
        {unused.map((n) => <StyleItem key={n} r={r} name={n} sel={sel} setSel={setSel} doc={doc} />)}
      </>)}
      <div>
        <div className="oadd" onClick={(e) => setMenu(!menu, e.currentTarget)}>＋ 新样式 ▾</div>
        {menu && (
          <div className="menu" style={menu} onMouseLeave={() => setMenu(false)}>
            {r.styles[curStyle] && <div className="oi" onClick={() => addStyle(freeName(curStyle), { ...r.styles[curStyle] })}>复制「{curStyle}」</div>}
            <div className="oi" onClick={() => { setMenu(false); setImporting(true); }}>从别的作品 / 预设 / 集导入…</div>
            <div className="dim" style={{ fontSize: 11.5, padding: "6px 8px 2px" }}>从出厂样式添加</div>
            {Object.entries(doc.factory).map(([k, f]) => (
              <div key={k} className={`oi${r.styles[k] ? " na" : ""}`} onClick={() => addStyle(r.styles[k] ? freeName(k) : k, { ...f, base: k })}>
                <Swatch st={f} /><div className="t">{k}<small>{FACTORY_LBL[k] ?? ""}{r.styles[k] ? " · 已有，会加成副本" : ""}</small></div>
              </div>
            ))}
          </div>
        )}
      </div>

      {importing && <ImportStyles r={r} doc={doc} onClose={() => setImporting(false)} onImport={(add, first) => {
        update((x) => ({ ...x, styles: { ...x.styles, ...add } }));
        setImporting(false);
        if (first) setSel(`s:${first}`);
      }} />}

      <div className="grp">通用</div>
      <div className={`oi${sel === "g:clean" ? " sel" : ""}`} onClick={() => setSel("g:clean")}>
        <div className="t">文字清理<small>{r.strip_marks.length ? `去掉 ${r.strip_marks.join(" ")}` : "不去掉符号"}</small></div></div>
    </div>
  );
}

function StyleItem({ r, name, sel, setSel, doc }: { r: R; name: string; sel: string; setSel: (s: string) => void; doc: ConfigDoc }) {
  const st = r.styles[name], n = usedBy(r, name).length;
  const mod = doc.kind === "episode" && !!templateChange(doc, ["render", "styles", name]);
  return (
    <div className={`oi${sel === `s:${name}` ? " sel" : ""}`} onClick={() => setSel(`s:${name}`)}>
      <Swatch st={st} />
      <div className="t">{name}{name === r.fallback_style && <span className="chip mini">兜底</span>}{mod && <span className="moddot" title="和模板不一样">●</span>}
        <small>{st.font} {st.size}{n ? ` · 用在 ${n} 处` : ""}</small></div>
    </div>
  );
}

/** 从别处导入样式：作品默认设置、预设、库里的其他集。同名的可以覆盖或者加成副本。 */
type Src = { id: string; label: string; group: string; load: () => Promise<Record<string, Style>> };
function ImportStyles({ r, doc, onClose, onImport }: { r: R; doc: ConfigDoc; onClose: () => void; onImport: (add: Record<string, Style>, first?: string) => void }) {
  const s = useStore();
  const [srcs, setSrcs] = useState<Src[]>([]);
  const [src, setSrc] = useState("");
  const [styles, setStyles] = useState<Record<string, Style> | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [same, setSame] = useState<"copy" | "replace">("copy");
  const stylesOf = (c: Cfg | undefined) => (c?.render?.styles ?? {}) as Record<string, Style>;
  useEffect(() => {
    Promise.all([api.series(), api.presets(), api.projects()]).then(([sr, pr, pj]) => setSrcs([
      ...sr.filter((x) => x.settings && !(doc.kind === "series" && x.name === doc.title))
        .map((x) => ({ id: `s:${x.name}`, label: x.name, group: "作品默认设置", load: () => api.seriesDoc(x.name).then((d) => stylesOf(d.effective)) })),
      ...pr.filter((x) => !(doc.kind === "preset" && x.name === doc.title))
        .map((x) => ({ id: `p:${x.id}`, label: `${x.name}${x.builtin ? "" : "（我的）"}`, group: "预设", load: () => api.preset(x.id).then((d) => stylesOf(d.effective)) })),
      ...pj.filter((x) => x.exists && x.configured !== false && !(doc.kind === "episode" && x.id === s.pid))
        .map((x) => ({ id: `e:${x.id}`, label: `${x.series ? `${x.series} › ` : ""}${x.name}`, group: "库里的集", load: () => api.config(x.id).then((c) => stylesOf(c.effective)) })),
    ])).catch((e) => s.setError(e.message));
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const x = srcs.find((y) => y.id === src);
    setStyles(null); setPicked([]);
    if (x) x.load().then(setStyles).catch((e) => s.setError(e.message));
  }, [src]);  // eslint-disable-line react-hooks/exhaustive-deps
  const groups = [...new Set(srcs.map((x) => x.group))];
  const nameFor = (n: string, taken: Set<string>) => {
    if (!taken.has(n) || same === "replace") return n;
    let k = 2; while (taken.has(`${n}${k}`)) k++;
    return `${n}${k}`;
  };
  const go = () => {
    if (!styles) return;
    const taken = new Set(Object.keys(r.styles));
    const add: Record<string, Style> = {};
    let first: string | undefined;
    for (const n of picked) { const to = nameFor(n, taken); taken.add(to); add[to] = { ...styles[n], base: styles[n].base || (to !== n && doc.factory[n] ? n : undefined) }; first ??= to; }
    onImport(add, first);
  };
  const clash = picked.filter((n) => r.styles[n]).length;
  return <Modal onClose={onClose} width={620}>
    <h3>导入样式</h3>
    <div className="kv" style={{ marginBottom: 10 }}>
      <span className="lbl">从</span>
      <select className="inp" value={src} onChange={(e) => setSrc(e.target.value)}>
        <option value="">选一个…</option>
        {groups.map((g) => <optgroup key={g} label={g}>{srcs.filter((x) => x.group === g).map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}</optgroup>)}
      </select>
    </div>
    {src && !styles && <div className="dim">加载中…</div>}
    {styles && (<>
      <div className="row" style={{ marginBottom: 6 }}><span className="dim">勾选要导入的样式</span><span className="sp" />
        <span className="link" onClick={() => setPicked(picked.length === Object.keys(styles).length ? [] : Object.keys(styles))}>{picked.length === Object.keys(styles).length ? "全不选" : "全选"}</span></div>
      <div className="usedby" style={{ maxHeight: 300, overflow: "auto" }}>
        {Object.entries(styles).map(([n, st]) => (
          <label key={n} className="row" style={{ cursor: "pointer" }}>
            <input type="checkbox" checked={picked.includes(n)} onChange={(e) => setPicked(e.target.checked ? [...picked, n] : picked.filter((x) => x !== n))} />
            <Swatch st={st} /><b style={{ fontWeight: 500 }}>{n}</b><span className="dim">{st.font} {st.size}</span>
            {r.styles[n] && <span className="chip mini warn">这里已有同名</span>}
          </label>
        ))}
        {!Object.keys(styles).length && <div className="dim">那里没有样式。</div>}
      </div>
      {clash > 0 && <div className="kv" style={{ marginTop: 10 }}><span className="lbl">同名的</span>
        <div className="seg"><button className={same === "copy" ? "on" : ""} onClick={() => setSame("copy")}>加成副本（改个名）</button>
          <button className={same === "replace" ? "on" : ""} onClick={() => setSame("replace")}>覆盖这里的</button></div></div>}
      <div className="hint" style={{ marginTop: 8 }}>导入的是那边现在的样子，之后两边各自独立。{same === "replace" && clash ? "覆盖后用这些样式的图层会马上变成导入的样子。" : ""}</div>
    </>)}
    <div className="row" style={{ marginTop: 12, justifyContent: "flex-end" }}>
      <button className="btn" onClick={onClose}>取消</button>
      <button className="btn pri" disabled={!picked.length} onClick={go}>导入 {picked.length || ""} 个</button>
    </div>
  </Modal>;
}

// ---------------- 字幕文件 ----------------
function FileEditor({ r, update, setSel, doc, i, li, setLi }: {
  r: R; update: Upd; setSel: (s: string) => void; doc: ConfigDoc; i: number; li: number; setLi: (k: number) => void;
}) {
  const s = useStore();
  const p = r.profiles[i];
  const [name, setName] = useState(p.name);
  useEffect(() => { setName(p.name); setLi(0); }, [i]);  // eslint-disable-line react-hooks/exhaustive-deps
  const src = s.data?.src ?? String(doc.effective.project?.source ?? "auto");
  const targets: string[] = s.data?.targets ?? doc.effective.project?.targets ?? [];
  const pack = (code: string) => s.langs.find((x) => x.code === code);
  const updP = (patch: Partial<Profile>) => update((x) => ({ ...x, profiles: x.profiles.map((q, j) => (j === i ? { ...q, ...patch } : q)) }));
  const updLayers = (layers: Layer[]) => updP({ layers });
  const layer = p.layers[li];
  const layerLang = (l: Layer) => (l.role === "original" ? src : l.role === "translation" ? l.lang || targets[0] : l.of === "original" ? src : l.of);
  const rename = () => {
    const n = name.trim();
    if (!n || n === p.name) { setName(p.name); return; }
    if (r.profiles.some((q, j) => j !== i && q.name === n)) { s.toast(`已经有叫「${n}」的字幕文件了`); setName(p.name); return; }
    const old = p.name;
    update((x) => ({ ...x, profiles: x.profiles.map((q, j) => (j === i ? { ...q, name: n } : q)),
      video: { ...x.video, profiles: x.video.profiles.map((v) => (v === old ? n : v)), default: x.video.default === old ? n : x.video.default, burn: x.video.burn === old ? n : x.video.burn } }));
  };
  const stem = (s.summary?.name ?? "视频名").replace(/\.[^.]+$/, "");
  const trLang = p.layers.find((l) => l.role === "translation")?.lang || targets[0] || "";
  const fname = p.filename.replace("{stem}", stem).replace("{tr}", trLang).replace("{src}", src).replace("{ext}", p.format).replace("{name}", p.name);
  const moveTo = (k: number, to: number) => {
    if (to < 0 || to >= p.layers.length || to === k) return;
    const ls = [...p.layers];
    const [m] = ls.splice(k, 1);
    ls.splice(to, 0, m);
    updLayers(ls);
    setLi(to);
  };
  // 拖动图层排序（用指针事件：桌面版接管了文件拖放，HTML5 拖放在 Windows 上收不到）
  const rows = useRef<(HTMLDivElement | null)[]>([]);
  const [drag, setDrag] = useState<{ k: number; to: number } | null>(null);
  const startDrag = (k: number, e: React.PointerEvent) => {
    if (e.button !== 0) return;
    e.preventDefault();
    let to = k;
    setDrag({ k, to });
    const move = (ev: PointerEvent) => {
      const i = rows.current.slice(0, p.layers.length).findIndex((el) => { const rc = el?.getBoundingClientRect(); return rc && ev.clientY < rc.top + rc.height / 2; });
      to = i < 0 ? p.layers.length : i;   // 插到第 to 个前面
      setDrag({ k, to });
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      setDrag(null);
      moveTo(k, to > k ? to - 1 : to);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const dropLine = (k: number) => !!drag && drag.to === k && drag.to !== drag.k && drag.to !== drag.k + 1;
  const styleNames = Object.keys(r.styles);
  return (<>
    <div className="card">
      <div className="row" style={{ marginBottom: 12 }}>
        <input className="inp title-inp" value={name} onChange={(e) => setName(e.target.value)} onBlur={rename} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
        <Sw on={p.enabled} onChange={(v) => updP({ enabled: v })} /><span className="dim">{p.enabled ? "输出" : "不输出"}</span>
        <span className="sp" />
        <button className="btn sm" onClick={() => {
          let n = 2; while (r.profiles.some((q) => q.name === `${p.name}-${n}`)) n++;
          update((x) => ({ ...x, profiles: [...x.profiles, { ...structuredClone(p), name: `${p.name}-${n}` }] }));
          setSel(`f:${r.profiles.length}`);
        }}>复制</button>
        <button className="btn sm ghost" disabled={r.profiles.length <= 1} onClick={() => {
          if (!confirm(`删除字幕文件「${p.name}」？`)) return;
          update((x) => ({ ...x, profiles: x.profiles.filter((_, j) => j !== i),
            video: { ...x.video, profiles: x.video.profiles.filter((v) => v !== p.name), default: x.video.default === p.name ? "" : x.video.default, burn: x.video.burn === p.name ? "" : x.video.burn } }));
          setSel("f:0");
        }}>删除</button>
      </div>
      <div className="kv">
        <span className="lbl">格式</span>
        <div className="seg">{(["ass", "srt"] as const).map((f) => <button key={f} className={p.format === f ? "on" : ""} onClick={() => updP({ format: f })}>{f.toUpperCase()}</button>)}</div>
        <span className="lbl">文件名</span>
        <div className="col" style={{ gap: 3 }}>
          <input className="inp" value={p.filename} onChange={(e) => updP({ filename: e.target.value })} />
          <span className="hint">→ {fname} <span className="dim">（可用 {"{stem}"} 视频名 {"{tr}"} 译文语言 {"{src}"} 原文语言 {"{ext}"} {"{name}"}）</span></span>
        </div>
      </div>
    </div>

    <div className="card">
      <div className="row" style={{ marginBottom: 8 }}><h3 style={{ margin: 0 }}>图层</h3><span className="hint">从上到下 = 画面中从上到下</span></div>
      <div className={`stack${drag ? " dragging" : ""}${dropLine(p.layers.length) ? " drop-end" : ""}`}>
        {p.layers.map((l, k) => (
          <div key={k} ref={(el) => { rows.current[k] = el; }} className={`lay${k === li ? " sel" : ""}${drag?.k === k ? " moving" : ""}${dropLine(k) ? " drop-before" : ""}`}>
            <div className="lh" onClick={() => setLi(k)}>
              <span className="grip" tabIndex={0} title="拖动调整上下顺序（也可以选中后按 ↑ ↓）" onPointerDown={(e) => startDrag(k, e)} onClick={(e) => e.stopPropagation()}
                    onKeyDown={(e) => { if (e.key === "ArrowUp" || e.key === "ArrowDown") { e.preventDefault(); moveTo(k, k + (e.key === "ArrowUp" ? -1 : 1)); } }}>⠿</span>
              <b>{ROLE_LBL[l.role]}</b><span className="dim">{langName(layerLang(l) ?? "", s.langs)}</span>
              <span className={`chip${r.styles[l.style] ? "" : " warn"}`} title={r.styles[l.style] ? "" : `没有这个样式，会用兜底样式「${r.fallback_style}」`}>{l.style}</span>
              {l.annotate.kind !== "none" && <span className="chip">{ANN_LBL[l.annotate.kind]}</span>}
              <span className="sp" />
              <div className="seg" onClick={(e) => e.stopPropagation()}>
                {(l.role === "original" ? (["on", "auto", "off"] as const) : (["on", "off"] as const)).map((v) =>
                  <button key={v} className={l.show === v ? "on" : ""} title={v === "auto" ? "画面上已经有原文硬字幕时不重复显示" : ""}
                          onClick={() => updLayers(p.layers.map((x, m) => (m === k ? { ...x, show: v } : x)))}>{{ on: "开", auto: "自动", off: "关" }[v]}</button>)}
              </div>
              <button className="del" title="删除图层" onClick={(e) => { e.stopPropagation(); updLayers(p.layers.filter((_, m) => m !== k)); setLi(0); }}>×</button>
            </div>
            {k === li && layer && (
              <div className="lb">
                <div className="kv">
                  {l.role === "translation" && (<><span className="lbl">语言</span>
                    <select className="inp" value={l.lang || targets[0] || ""} onChange={(e) => {
                      const pk = pack(e.target.value);
                      updLayers(p.layers.map((x, m) => (m === k ? { ...x, lang: e.target.value, annotate: pk?.annotations.includes(x.annotate.kind) ? x.annotate : { ...x.annotate, kind: "none" } } : x)));
                    }}>{targets.map((t) => <option key={t} value={t}>{langName(t, s.langs)}</option>)}</select></>)}
                  {l.role === "original" && (<><span className="lbl">语言</span><span className="muted">{src === "auto" ? "源语言（自动判断）" : `${langName(src, s.langs)}（源语言）`}</span></>)}
                  {l.role === "transliteration" && (<><span className="lbl">转写对象</span>
                    <select className="inp" value={l.of} onChange={(e) => updLayers(p.layers.map((x, m) => (m === k ? { ...x, of: e.target.value } : x)))}>
                      {[["original", src], ...targets.map((t) => [t, t])].filter(([, c]) => pack(c)?.translit).map(([v, c]) =>
                        <option key={v} value={v}>{v === "original" ? "原文" : "译文"}（{langName(c, s.langs)}）→ {pack(c)?.translit}</option>)}
                    </select>
                    {pack(layerLang(l))?.code.startsWith("zh") && (<><span className="lbl">声调</span>
                      <div className="seg">{([["mark", "pīn"], ["number", "pin1"], ["none", "pin"]] as const).map(([v, t]) =>
                        <button key={v} className={l.tone === v ? "on" : ""} onClick={() => updLayers(p.layers.map((x, m) => (m === k ? { ...x, tone: v } : x)))}>{t}</button>)}</div></>)}</>)}
                  <span className="lbl">样式</span>
                  <div className="row">
                    <select className="inp" value={l.style} onChange={(e) => updLayers(p.layers.map((x, m) => (m === k ? { ...x, style: e.target.value } : x)))}>
                      {!r.styles[l.style] && <option value={l.style}>{l.style}（没有这个样式）</option>}
                      {styleNames.map((n) => <option key={n} value={n}>{n}</option>)}
                    </select>
                    <span className="link" onClick={() => setSel(`s:${r.styles[l.style] ? l.style : r.fallback_style}`)}>编辑这个样式 →</span>
                  </div>
                  {l.role !== "transliteration" && (<><span className="lbl">每行字数</span>
                    <div className="row"><input type="range" min={0} max={60} value={l.max_chars} onChange={(e) => updLayers(p.layers.map((x, m) => (m === k ? { ...x, max_chars: Number(e.target.value) } : x)))} />
                      <span className="dim" style={{ width: 70 }}>{l.max_chars || "语言默认"}</span></div></>)}
                </div>
                {l.role !== "transliteration" && <AnnotateBox layer={l} pack={pack(layerLang(l) ?? "")} baseColor={r.styles[l.style]?.color ?? "#ffffff"}
                  onChange={(a) => updLayers(p.layers.map((x, m) => (m === k ? { ...x, annotate: { ...x.annotate, ...a } } : x)))} />}
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="row wrap" style={{ marginTop: 8 }}>
        <button className="btn sm" onClick={() => { updLayers([...p.layers, newLayer("translation", r.fallback_style)]); setLi(p.layers.length); }}>＋ 译文层</button>
        <button className="btn sm" onClick={() => { updLayers([...p.layers, newLayer("original", r.styles.sub ? "sub" : r.fallback_style)]); setLi(p.layers.length); }}>＋ 原文层</button>
        <button className="btn sm" onClick={() => { updLayers([...p.layers, newLayer("transliteration", r.styles.translit ? "translit" : r.fallback_style)]); setLi(p.layers.length); }}>＋ 转写层</button>
      </div>
    </div>

    <div className="card">
      <h3>排版</h3>
      <div className="kv">
        <span className="lbl">最多行数</span>
        <div className="seg">{[1, 2, 3].map((n) => <button key={n} className={p.max_lines === n ? "on" : ""} onClick={() => updP({ max_lines: n })}>{n}</button>)}</div>
        <span className="lbl">避开硬字幕</span>
        <label className="row"><input type="checkbox" checked={p.avoid_hardsub} onChange={(e) => updP({ avoid_hardsub: e.target.checked })} />画面下方已经有字时，字幕往上让</label>
        <span className="lbl">同时出现</span>
        <div className="row">
          <div className="seg">{([["stack", "分开显示"], ["merge", "合成一屏"]] as const).map(([v, t]) =>
            <button key={v} className={(p.simultaneous ?? "stack") === v ? "on" : ""} onClick={() => updP({ simultaneous: v })}>{t}</button>)}</div>
          <span className="hint">{(p.simultaneous ?? "stack") === "stack" ? "几条几乎同时开始时，每条单独一块、上下错开" : "放在同一屏里，每条的译文和原文仍挨在一起"}</span>
        </div>
        <span className="lbl">说明文字</span>
        <div className="row wrap">
          <div className="seg">{([["near", "贴原文旁"], ["top", "顶部"], ["off", "不显示"]] as const).map(([v, t]) =>
            <button key={v} className={p.info_mode === v ? "on" : ""} onClick={() => updP({ info_mode: v })}>{t}</button>)}</div>
          {p.format === "ass" && p.info_mode !== "off" && (<>
            <span className="dim">样式</span>
            <select className="inp" style={{ minWidth: 120 }} value={p.info_style || "info"} onChange={(e) => updP({ info_style: e.target.value })}>
              {!r.styles[p.info_style || "info"] && <option value={p.info_style || "info"}>{p.info_style || "info"}（没有这个样式）</option>}
              {styleNames.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
            <span className="link" onClick={() => setSel(`s:${r.styles[p.info_style || "info"] ? p.info_style || "info" : r.fallback_style}`)}>编辑 →</span>
          </>)}
          {p.format === "ass" && p.info_mode === "near" && (
            <label className="row" title="原文是竖排的人名条、说明：译文（中文、日文）也竖排，贴在竖条外侧，不容易压到人">
              <input type="checkbox" checked={p.info_vertical ?? true} onChange={(e) => updP({ info_vertical: e.target.checked })} />竖排的也竖排
            </label>
          )}
        </div>
        <span /><span className="hint">说明文字 = 人名条、地名、标题这些画面上的说明。</span>
      </div>
    </div>
  </>);
}

function AnnotateBox({ layer, pack, baseColor, onChange }: { layer: Layer; pack?: LangInfo; baseColor: string; onChange: (a: Partial<Annotate>) => void }) {
  const a = layer.annotate;
  if (!pack?.annotations.length) return <div className="hint" style={{ marginTop: 8 }}>{pack?.name ?? "这门语言"}没有可用的注音。</div>;
  return (
    <div className="kv" style={{ marginTop: 10 }}>
      <span className="lbl">注音</span>
      <div className="seg">{["none", ...pack.annotations].map((k) => <button key={k} className={a.kind === k ? "on" : ""} onClick={() => onChange({ kind: k as Annotate["kind"] })}>{ANN_LBL[k]}</button>)}</div>
      {a.kind === "furigana" && (<>
        <span className="lbl">范围</span>
        <div className="seg">{([["all", "全部"], ["difficult", "难读"], ["proper", "专有名词"], ["first", "首次出现"]] as const).map(([v, l]) => <button key={v} className={a.scope === v ? "on" : ""} onClick={() => onChange({ scope: v })}>{l}</button>)}</div>
        {a.scope === "difficult" && (<><span className="lbl">简单汉字</span>
          <div className="seg">{([["grade1", "小学一年级"], ["grade2", "一、二年级"]] as const).map(([v, l]) => <button key={v} className={a.easy_kanji === v ? "on" : ""} onClick={() => onChange({ easy_kanji: v })}>{l}</button>)}</div>
          <span className="lbl">已会的词</span><input className="inp ja" placeholder="用空格分隔，这些词不注音" value={a.known_words.join(" ")} onChange={(e) => onChange({ known_words: e.target.value.split(/\s+/).filter(Boolean) })} /></>)}
        <span className="lbl">文字</span>
        <div className="seg">{([["hiragana", "ひらがな"], ["katakana", "カタカナ"], ["romaji", "romaji"]] as const).map(([v, l]) => <button key={v} className={a.script === v ? "on" : ""} onClick={() => onChange({ script: v })}>{l}</button>)}</div>
        <span className="lbl">数字</span><label className="row dim"><input type="checkbox" checked={a.numbers} onChange={(e) => onChange({ numbers: e.target.checked })} />数字也注音（读音易错）</label>
      </>)}
      {a.kind === "pinyin" && (<><span className="lbl">声调</span>
        <div className="seg">{([["mark", "pīn yīn"], ["number", "pin1 yin1"], ["none", "pin yin"]] as const).map(([v, l]) => <button key={v} className={a.tone === v ? "on" : ""} onClick={() => onChange({ tone: v })}>{l}</button>)}</div></>)}
      {a.kind !== "none" && (<>
        <span className="lbl">大小 / 颜色</span>
        <div className="row"><input type="range" min={0.3} max={0.7} step={0.02} value={a.size} onChange={(e) => onChange({ size: Number(e.target.value) })} />
          <input type="color" value={a.color || baseColor} onChange={(e) => onChange({ color: e.target.value })} />
          {a.color && <button className="btn sm ghost" onClick={() => onChange({ color: "" })}>同底字</button>}</div>
        <span className="lbl">SRT</span>
        <div className="seg">{([["paren", "漢字(かんじ)"], ["none", "不输出"]] as const).map(([v, l]) => <button key={v} className={a.srt === v ? "on" : ""} onClick={() => onChange({ srt: v })}>{l}</button>)}</div>
      </>)}
    </div>
  );
}

function BroadcastEditor({ r, update, doc, bc, subsRead }: { r: R; update: Upd; doc: ConfigDoc; bc: { file: string | null } | null; subsRead: boolean }) {
  const s = useStore();
  const na = doc.kind === "episode" && !bc;
  const stem = (s.summary?.name ?? "视频名").replace(/\.[^.]+$/, "");
  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 12 }}><h2 className="ed-title">广播字幕原样</h2>
        <Sw on={r.broadcast && !na} disabled={na} onChange={(v) => update((x) => ({ ...x, broadcast: v }))} /><span className="dim">{na ? "没有广播字幕" : r.broadcast ? "输出" : "不输出"}</span></div>
      <div className="hint" style={{ marginBottom: 10 }}>字幕来自电视广播（4K 的 ARIB-TTML、2K 的 ARIB）时才有：日语原文，位置、字号、颜色、半透明底色、振假名照搬电视上的样子，时间按字幕对时的偏移挪过。
        {na && <b style={{ color: "var(--warn)" }}> {subsRead ? "这个视频的字幕不是电视广播字幕（或者「处理 › 字幕」里没用视频里的字幕）。" : "正在检查视频里有没有电视广播字幕…"}</b>}</div>
      <div className="kv">
        <span className="lbl">文件名</span><span className="muted">{bc?.file ?? `${stem}.ja-broadcast.ass`}</span>
        <span className="lbl">字体</span>
        <div className="row"><input className="inp" list="out-fonts" placeholder="自动（圆体 > 等宽的日文字体）" value={r.broadcast_font} onChange={(e) => update((x) => ({ ...x, broadcast_font: e.target.value }))} /></div>
      </div>
      <datalist id="out-fonts">{FONTS.map((f) => <option key={f} value={f} />)}</datalist>
    </div>
  );
}

// ---------------- 视频 ----------------
function VideoEditor({ r, update, doc, mode, bc }: { r: R; update: Upd; doc: ConfigDoc; mode: "mux" | "burn"; bc: { file: string | null } | null }) {
  const s = useStore();
  const vc = r.video;
  const setV = (patch: Partial<Video>) => update((x) => ({ ...x, video: { ...x.video, ...patch } }));
  const hasBc = doc.kind !== "episode" || !!bc;
  const rows = [
    ...r.profiles.filter((p) => p.enabled).map((p) => ({ id: p.name, label: p.name, fmt: p.format.toUpperCase() })),
    ...(r.broadcast && hasBc ? [{ id: "broadcast", label: "广播字幕原样", fmt: "ASS" }] : []),
  ];
  const ids = rows.map((x) => x.id);
  const muxed = (vc.profiles.length ? vc.profiles : ids.filter((id) => id !== "broadcast" || vc.broadcast)).filter((id) => ids.includes(id));
  const setMux = (id: string, on: boolean) => { const base = vc.profiles.length ? vc.profiles : muxed; setV({ profiles: on ? [...base.filter((x) => x !== id), id] : base.filter((x) => x !== id) }); };
  const defId = muxed.includes(vc.default) ? vc.default : rows.find((x) => muxed.includes(x.id))?.id;
  const burnId = ids.includes(vc.burn) ? vc.burn : rows.find((x) => x.id !== "broadcast" && x.fmt === "ASS")?.id;
  const episode = doc.kind === "episode";
  const ready = episode && !!s.data?.cues.length && !s.running;
  const p = s.progress && s.progress.stage === mode ? s.progress : null;
  const done = s.summary?.videos?.[mode];
  const stem = (s.summary?.name ?? "视频名").replace(/\.[^.]+$/, "");
  const auto = vc.mode === mode;
  return (<>
    <div className="card">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 className="ed-title">{mode === "mux" ? "封装 MKV" : "压制硬字幕"}</h2><span className="sp" />
        {episode && <button className="btn" disabled={!ready || (mode === "burn" && !s.summary?.meta?.video)} title={!s.data?.cues.length ? "先运行出字幕" : ""}
                            onClick={() => s.exportVideo(mode)}>现在{mode === "mux" ? "封装" : "压制"}</button>}
      </div>
      <div className="kv">
        <span className="lbl">运行完自动</span>
        <div className="row"><Sw on={auto} onChange={(v) => setV({ mode: v ? mode : "off" })} />
          <span className="hint">{mode === "mux" ? "字幕轨 + 用到的字体放进 MKV，不重新编码，几秒完成" : "字幕画进画面，任何播放器都能看，要重新编码，比较慢"}
            {!auto && vc.mode !== "off" ? `（现在是运行完自动${vc.mode === "mux" ? "封装" : "压制"}，打开这个会换成这个）` : ""}</span></div>
        <span className="lbl">输出到</span><span className="muted">{stem}.{mode === "mux" ? "sub.mkv" : "hardsub.mp4"}{done ? <span className="chip ok" style={{ marginLeft: 8 }}>已输出</span> : null}</span>
      </div>
      {p && s.running && (
        <div className="row" style={{ marginTop: 10 }}>
          <div className="pbar" style={{ flex: 1 }}><div style={{ width: `${pct(p)}%` }} /></div>
          <span className="dim">{STAGE_LBL[p.stage] ?? p.stage} {pct(p)}%</span><button className="btn sm" onClick={s.cancel}>取消</button>
        </div>
      )}
    </div>
    <div className="card">
      <h3>{mode === "mux" ? "放进哪些字幕" : "压哪一个"}</h3>
      {rows.length === 0 ? <div className="dim">没有要输出的字幕文件：在左边打开一个字幕文件的开关。</div> : (
        <table className="tb"><thead><tr><th style={{ width: 80 }}>{mode === "mux" ? "放进 MKV" : ""}</th><th>字幕文件</th>{mode === "mux" && <th>默认轨</th>}</tr></thead><tbody>
          {rows.map((x) => (
            <tr key={x.id}>
              <td>{mode === "mux" ? <input type="checkbox" checked={muxed.includes(x.id)} onChange={(e) => setMux(x.id, e.target.checked)} />
                : <input type="radio" name="burn" checked={burnId === x.id} onChange={() => setV({ burn: x.id })} />}</td>
              <td>{x.label} <span className="dim">· {x.fmt}</span>{mode === "burn" && x.fmt === "SRT" && <span className="hint">（用它第一个图层的样式）</span>}</td>
              {mode === "mux" && <td><input type="radio" name="vdefault" disabled={!muxed.includes(x.id)} checked={defId === x.id} onChange={() => setV({ default: x.id })} /></td>}
            </tr>
          ))}
        </tbody></table>)}
      <div className="hint" style={{ marginTop: 6 }}>只列出要输出的字幕文件；不输出的在左边打开开关后才会出现在这里。</div>
    </div>
    <div className="card">
      <h3>{mode === "mux" ? "封装选项" : "编码"}</h3>
      <div className="kv">
        {mode === "mux" ? (<>
          <span className="lbl">字体</span>
          <select className="inp" value={vc.fonts} onChange={(e) => setV({ fonts: e.target.value })}>{Object.entries(FONTS_LBL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          <span className="lbl">原有字幕轨</span>
          <label className="row"><input type="checkbox" checked={vc.keep_subs} onChange={(e) => setV({ keep_subs: e.target.checked })} />保留原视频里的字幕轨</label>
        </>) : (<>
          <span className="lbl">编码</span>
          <select className="inp" value={vc.encoder} onChange={(e) => setV({ encoder: e.target.value })}>{ENCODERS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          <span className="lbl">画质</span>
          <div className="row"><input type="range" min={14} max={32} step={1} value={vc.quality} onChange={(e) => setV({ quality: Number(e.target.value) })} />
            <span className="dim">{vc.quality} · 越小越清晰，常用 18~23</span></div>
          <span className="lbl">尺寸</span>
          <select className="inp" value={vc.height} onChange={(e) => setV({ height: Number(e.target.value) })}>
            {[[0, "原尺寸"], [2160, "2160p"], [1080, "1080p"], [720, "720p"], [480, "480p"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
        </>)}
      </div>
    </div>
  </>);
}

// ---------------- 样式 ----------------
function StyleEditor({ r, update, setSel, doc, name }: { r: R; update: Upd; setSel: (s: string) => void; doc: ConfigDoc; name: string }) {
  const s = useStore();
  const st = r.styles[name];
  const [nm, setNm] = useState(name);
  const [del, setDel] = useState(false);
  useEffect(() => setNm(name), [name]);
  const used = usedBy(r, name);
  const fb = name === r.fallback_style;
  const facKey = st.base && doc.factory[st.base] ? st.base : doc.factory[name] ? name : "";
  const fac = facKey ? doc.factory[facKey] : null;
  const upd = (patch: Partial<Style>) => update((x) => ({ ...x, styles: { ...x.styles, [name]: { ...x.styles[name], ...patch } } }));
  const unsaved = useUnsaved();
  const tplStyles = doc.template?.effective?.render?.styles as Record<string, Style> | undefined;
  const tplHas = !!tplStyles?.[name];
  const tplMod = doc.kind === "episode" && !!templateChange(doc, ["render", "styles", name]);
  const rename = () => {
    const n = nm.trim();
    if (n === name) return;
    if (!n || n.includes(",")) { s.toast("样式名不能是空的，也不能有逗号"); setNm(name); return; }
    if (r.styles[n]) { s.toast(`已经有叫「${n}」的样式了`); setNm(name); return; }
    update((x) => renameStyle(x, name, n));
    setSel(`s:${n}`);
  };
  return (<>
    <div className="card">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 className="ed-title">样式</h2>
        {fb && <span className="chip">兜底样式</span>}
        <span className="sp" />
        <button className="btn sm" onClick={() => { let n = 2; while (r.styles[`${name}${n}`]) n++; update((x) => ({ ...x, styles: { ...x.styles, [`${name}${n}`]: { ...st } } })); setSel(`s:${name}${n}`); }}>复制</button>
        <button className="btn sm ghost" disabled={fb} title={fb ? "图层找不到样式时用它。先把别的样式设成兜底，才能删这个" : ""} onClick={() => setDel(true)}>删除</button>
      </div>
      <div className="kv">
        <span className="lbl">名字</span>
        <div className="col" style={{ gap: 3 }}>
          <input className="inp" style={{ maxWidth: 220 }} value={nm} onChange={(e) => setNm(e.target.value)} onBlur={rename} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
          <span className="hint">就是 ASS 里的样式名，可以用中文（不能有逗号）。改名时，用它的图层、说明文字一起改。</span>
        </div>
        <span className="lbl">来自</span>
        <span className="muted">{fac ? `出厂样式「${facKey}」${FACTORY_LBL[facKey] ? `（${FACTORY_LBL[facKey]}）` : ""}` : "自己新建"}</span>
        <span className="lbl">字体</span>
        <input className="inp" list="out-fonts" value={st.font} onChange={(e) => upd({ font: e.target.value })} />
        <span className="lbl">字号</span>
        <div className="row"><input type="range" min={14} max={120} value={st.size} onChange={(e) => upd({ size: Number(e.target.value) })} /><span className="dim" style={{ width: 28 }}>{st.size}</span></div>
        <span className="lbl">文字</span>
        <div className="row"><input type="color" value={st.color} onChange={(e) => upd({ color: e.target.value })} />
          <label className="row dim"><input type="checkbox" checked={st.bold} onChange={(e) => upd({ bold: e.target.checked })} />粗</label>
          <label className="row dim"><input type="checkbox" checked={st.italic} onChange={(e) => upd({ italic: e.target.checked })} />斜</label></div>
        <span className="lbl">底色框</span>
        <div className="row wrap"><label className="row dim"><input type="checkbox" checked={!!st.box} onChange={(e) => upd({ box: e.target.checked })} />文字后面垫底色（没有描边）</label>
          {st.box && (<><input type="color" value={st.box_color ?? "#000000"} onChange={(e) => upd({ box_color: e.target.value })} />
            <span className="dim">不透明</span><input type="range" min={0.1} max={1} step={0.05} value={st.box_opacity ?? 0.6} onChange={(e) => upd({ box_opacity: Number(e.target.value) })} />
            <span className="dim">{Math.round((st.box_opacity ?? 0.6) * 100)}%</span></>)}</div>
        <span className="lbl">{st.box ? "框的留边" : "描边"}</span>
        <div className="row">{!st.box && <input type="color" value={st.outline_color} onChange={(e) => upd({ outline_color: e.target.value })} />}
          <input type="range" min={0} max={st.box ? 20 : 8} step={0.2} value={st.outline} onChange={(e) => upd({ outline: Number(e.target.value) })} /><span className="dim">{st.outline}</span></div>
        <span className="lbl">阴影</span>
        <div className="row"><input type="range" min={0} max={6} step={0.5} value={st.shadow} onChange={(e) => upd({ shadow: Number(e.target.value) })} /><span className="dim">{st.shadow}</span></div>
        <span className="lbl">字间距</span>
        <div className="row"><input type="range" min={-4} max={12} step={0.5} value={st.spacing ?? 0} onChange={(e) => upd({ spacing: Number(e.target.value) })} /><span className="dim">{st.spacing ?? 0}</span></div>
      </div>
      <div className="row wrap restore">
        <span className="dim">恢复：</span>
        {doc.kind === "episode" && doc.template?.kind && (
          <button className={`btn sm${tplMod ? " tplb" : ""}`} disabled={!tplHas || !tplMod || unsaved}
                  title={!tplHas ? "模板里没有这个样式" : !tplMod ? "和模板里一样" : "变回模板（作品默认设置 / 预设）里的样子"}
                  onClick={() => doc.applyTemplate?.([["render", "styles", name]], "episode")}>恢复成模板里的样子</button>)}
        <button className="btn sm" disabled={!fac} title={fac ? "变回软件自带的样子（名字不变）" : "自己新建的样式没有出厂值"}
                onClick={() => fac && upd({ ...fac, base: facKey === name ? undefined : facKey })}>恢复出厂{fac ? `（${facKey}）` : ""}</button>
        <span className="sp" />
        {!fb && <button className="btn sm ghost" onClick={() => update((x) => ({ ...x, fallback_style: name }))}>设为兜底样式</button>}
      </div>
    </div>
    <div className="card">
      <h3>用在 {used.length} 处 <span className="dim" style={{ fontWeight: 400 }}>改这里，这些地方一起变</span></h3>
      <div className="usedby">
        {used.map((u, k) => <div key={k}>{u.label}<span className="sp" /><span className="link" onClick={() => setSel(u.sel)}>去看 →</span></div>)}
        {fb && <div className="muted">兜底：图层、说明文字用的样式不存在时用它</div>}
        {!used.length && !fb && <div className="dim">没有地方在用。可以删掉，以后要用再从出厂样式加回来。</div>}
      </div>
    </div>
    <datalist id="out-fonts">{FONTS.map((f) => <option key={f} value={f} />)}</datalist>
    {del && <DeleteStyle r={r} name={name} fac={facKey} onClose={() => setDel(false)} onDelete={(rep) => {
      update((x) => deleteStyle(x, name, rep));
      setDel(false);
      setSel(`s:${rep ?? r.fallback_style}`);
    }} />}
  </>);
}

function renameStyle(x: R, from: string, to: string): R {
  const styles = Object.fromEntries(Object.entries(x.styles).map(([k, v]) => (k === from ? [to, { ...v, base: v.base || (from in FACTORY_LBL ? from : v.base) }] : [k, v])));
  return { ...x, styles, fallback_style: x.fallback_style === from ? to : x.fallback_style, profiles: replaceStyleRefs(x.profiles, from, to) };
}

function deleteStyle(x: R, name: string, rep: string | null): R {
  const styles = { ...x.styles };
  delete styles[name];
  return { ...x, styles, profiles: rep ? replaceStyleRefs(x.profiles, name, rep) : x.profiles };
}

function replaceStyleRefs(profiles: Profile[], from: string, to: string): Profile[] {
  return profiles.map((p) => ({ ...p, info_style: (p.info_style || "info") === from ? to : p.info_style,
    layers: p.layers.map((l) => (l.style === from ? { ...l, style: to } : l)) }));
}

function DeleteStyle({ r, name, fac, onClose, onDelete }: { r: R; name: string; fac: string; onClose: () => void; onDelete: (rep: string | null) => void }) {
  const used = usedBy(r, name);
  const others = Object.keys(r.styles).filter((k) => k !== name);
  const [rep, setRep] = useState(others.includes(r.fallback_style) ? r.fallback_style : others[0]);
  return <Modal onClose={onClose}>
    <h3>删除样式「{name}」</h3>
    {used.length ? (<>
      <div className="hint" style={{ marginBottom: 8 }}>还有 {used.length} 处在用：</div>
      <div className="usedby" style={{ marginBottom: 10 }}>{used.map((u, k) => <div key={k}>{u.label}</div>)}</div>
      <div className="kv"><span className="lbl">改用</span>
        <select className="inp" value={rep} onChange={(e) => setRep(e.target.value)}>{others.map((k) => <option key={k} value={k}>{k}</option>)}</select></div>
    </>) : <div className="hint">没有地方在用，可以直接删。</div>}
    <div className="hint" style={{ marginTop: 8 }}>{fac ? `删了以后要用，可以“＋ 新样式 › 从出厂样式添加”把「${fac}」加回来（是出厂的样子，不是现在改过的样子）。` : "这是自己新建的样式，删了就没有了。"}</div>
    <div className="row" style={{ marginTop: 12, justifyContent: "flex-end" }}>
      <button className="btn" onClick={onClose}>取消</button>
      <button className="btn pri" onClick={() => onDelete(used.length ? rep : null)}>{used.length ? "替换并删除" : "删除"}</button>
    </div>
  </Modal>;
}

function CleanEditor({ r, update }: { r: R; update: Upd }) {
  const [text, setText] = useState(r.strip_marks.join(" "));
  return (
    <div className="card">
      <h2 className="ed-title" style={{ marginBottom: 12 }}>文字清理</h2>
      <div className="kv">
        <span className="lbl">去掉的符号</span>
        <div className="col" style={{ gap: 3 }}>
          <input className="inp" value={text} placeholder="例如 ➡" onChange={(e) => { setText(e.target.value); update((x) => ({ ...x, strip_marks: e.target.value.split(/\s+/).filter(Boolean) })); }} />
          <span className="hint">原文、译文里出现这些符号时去掉，空格分隔；对所有字幕文件生效。默认去掉日文字幕行尾表示“接下一条”的 ➡。</span>
        </div>
      </div>
    </div>
  );
}

// ---------------- 预览 ----------------
function OutPreview({ profile, exportable, hl, hlLabel }: { profile?: string; exportable?: string; hl?: PreviewHl; hlLabel: string }) {
  const s = useStore();
  const d = s.data;
  const preview = usePreview(profile);
  const player = useRef<PlayerHandle>(null);
  const [, setTime] = useState(0);
  const onTime = useCallback((t: number) => setTime(t), []);
  const firstT = d ? (d.cues[0]?.start ?? d.subs[0]?.start ?? d.ocr[0]?.start) : undefined;
  const jumped = useRef(false);
  useEffect(() => {
    if (jumped.current || firstT === undefined) return;
    jumped.current = true;
    if (s.pid && lastPos(s.pid) > 0.5) return;   // 播过的：播放器自己跳回上次的地方
    const t = setTimeout(() => player.current?.seek(firstT + 0.5), 400);
    return () => clearTimeout(t);
  }, [firstT]);
  if (!d) return <div className="card dim">加载中…</div>;
  const pick = <T,>(xs: T[]) => xs.filter((_, i) => i % Math.max(1, Math.floor(xs.length / 6)) === 0).slice(0, 6);
  const samples = d.cues.length ? pick(d.cues).map((c) => ({ t: c.start + 0.3, label: c.orig.text.slice(0, 8) }))
    : pick(d.subs.length ? d.subs : d.ocr).map((c) => ({ t: c.start + 0.3, label: c.text.slice(0, 8) }));
  return (
    <div className="card outprev">
      <div className="row" style={{ marginBottom: 8 }}><h3 style={{ margin: 0 }}>预览</h3><span className="dim">{profile ?? ""}</span><span className="sp" />
        {hlLabel && <span className="hint" title="画面上虚线框出来的是正在改的这一项；这一刻没有它的字幕时看不到框，可以点下面的时间跳过去">虚线框：{hlLabel}</span>}</div>
      <Player ref={player} preview={preview} onTime={onTime} controls hl={hl} />
      {samples.length > 0 && (
        <div className="row wrap" style={{ marginTop: 8 }}>
          <span className="dim">跳到：</span>
          {samples.map((c, i) => <button key={i} className="btn sm" onClick={() => player.current?.seek(c.t)}>{fmt(c.t).slice(0, 5)}{c.label ? ` ${c.label}` : ""}</button>)}
        </div>
      )}
      <div className="row" style={{ marginTop: 10 }}>
        {exportable && <button className="btn pri" onClick={async () => {
          if (!s.pid) return;
          try { const res = await api.export(s.pid, [exportable]); s.toast(`已导出：${res.files.join("、")}`); } catch (e) { s.setError((e as Error).message); }
        }}>导出这个文件</button>}
        <span className="hint">预览和导出的 ASS 用同一套排版；带注音的 ASS 播放端需要有同一字体。</span>
      </div>
    </div>
  );
}
