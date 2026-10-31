// 一集页头的运行条：▶ 运行，旁边写下次运行会重做哪几步；运行到、强制重做、不用缓存、清掉修改收在 ▾ 里。
import { useEffect, useRef, useState } from "react";
import { useUnsaved } from "../doc";
import { RUN_DEFAULT, useStore } from "../store";
import { STAGE_LBL } from "../util";

export const REDO_STAGES = ["subs", "asr", "ocr", "ocr_refine", "align", "fuse", "translate", "annotate"];
const UNTIL = ["subs", "asr", "ocr", "fuse", "translate", "annotate", "render"];

export function RunBar() {
  const s = useStore();
  const o = s.runOpts;
  const unsaved = useUnsaved();
  const [menu, setMenu] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menu) return;
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setMenu(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [menu]);
  const stale = (s.summary?.plan ?? []).filter((p) => !["done", "off", "empty"].includes(p.status)).map((p) => STAGE_LBL[p.stage] ?? p.stage);
  const all = o.redo.includes("all");
  const forced = all ? ["全部步骤"] : REDO_STAGES.filter((x) => o.redo.includes(x)).map((x) => STAGE_LBL[x]);
  const extra = [o.until !== "render" ? `运行到${STAGE_LBL[o.until]}` : "", forced.length ? `强制重做：${forced.join("、")}` : "",
    o.noCache && o.redo.length ? "不用缓存" : "", o.clearEdits ? "清掉手动修改" : ""].filter(Boolean);
  const start = () => {
    const body = [...o.redo, ...(o.redo.length && o.noCache ? ["no_cache"] : []), ...(o.clearEdits ? ["clear_edits"] : [])];
    s.setRunOpts((x) => ({ ...RUN_DEFAULT, until: x.until }));  // 重做、清掉修改只对这一次有效
    s.run({ until: o.until, redo: body });
  };
  return (
    <div className="runbar" ref={box}>
      <span className={`pending${s.running ? " muted" : ""}`}>
        {s.running ? "正在运行：现在改的设置下次运行才生效"
          : unsaved ? "正在保存设置…"
          : stale.length ? `已自动保存。下次运行会做：${stale.join(" → ")}` : "已自动保存。都是最新的"}
        {extra.length > 0 && !s.running && <span className="acc">（{extra.join("；")}）</span>}
      </span>
      {s.running && <button className="btn" onClick={s.cancel}>■ 停止</button>}
      <div className="runbtn">
        <button className="btn pri" disabled={s.running || unsaved} onClick={start}>{s.running ? "运行中…" : "▶ 运行"}</button>
        <button className="btn pri runmore" disabled={s.running} title="运行到哪一步、强制重做…" onClick={() => setMenu(!menu)}>▾</button>
      </div>
      {menu && (
        <div className="menu runmenu">
          <div className="kv" style={{ gridTemplateColumns: "64px 1fr" }}>
            <span className="lbl">运行到</span>
            <select className="inp" value={o.until} onChange={(e) => s.setRunOpts((x) => ({ ...x, until: e.target.value }))}>
              {UNTIL.map((x) => <option key={x} value={x}>{STAGE_LBL[x]}{x === "render" ? "（全部）" : ""}</option>)}
            </select>
          </div>
          <hr />
          <div className="dim" style={{ fontSize: 12, marginBottom: 4 }}>强制重做（平时不用：改了设置会自动重做受影响的步骤；也可以在「处理」里每一步右上角点“下次重做这一步”）</div>
          <label className="row"><input type="checkbox" checked={all} onChange={() => s.setRunOpts((x) => ({ ...x, redo: all ? [] : ["all"] }))} /><b>全部步骤</b></label>
          <div className="redo-grid">
            {REDO_STAGES.map((x) => (
              <label key={x} className="row"><input type="checkbox" checked={all || o.redo.includes(x)} disabled={all}
                onChange={() => s.setRunOpts((y) => ({ ...y, redo: y.redo.includes(x) ? y.redo.filter((z) => z !== x) : [...y.redo.filter((z) => z !== "all"), x] }))} />{STAGE_LBL[x]}</label>
            ))}
          </div>
          <hr />
          <label className="row" title="勾选重做的步骤里，大模型翻译、视觉复核、云端语音识别重新请求，不用以前存下的结果（会再花一次钱）">
            <input type="checkbox" checked={o.noCache} disabled={!o.redo.length} onChange={(e) => s.setRunOpts((x) => ({ ...x, noCache: e.target.checked }))} />重做的步骤不用缓存（重新请求，会再花钱）</label>
          <label className="row" style={o.clearEdits ? { color: "var(--warn)" } : undefined}
                 title="清掉审校时的所有手动修改，完全按这次识别和翻译的结果来。会先备份到项目文件夹的 backup 里。术语表不受影响。">
            <input type="checkbox" checked={o.clearEdits} onChange={(e) => s.setRunOpts((x) => ({ ...x, clearEdits: e.target.checked }))} />清掉审校时的手动修改（先备份）</label>
        </div>
      )}
    </div>
  );
}
