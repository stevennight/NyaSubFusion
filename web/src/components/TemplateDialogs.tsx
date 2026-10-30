// 和模板比较（逐项恢复 / 存到模板）、存成预设、把作品默认设置的改动同步给已有的集。
import { useEffect, useState } from "react";
import { api } from "../api";
import { useUnsaved, type ConfigDoc } from "../doc";
import { settingLabel, showVal } from "../settingLabels";
import { useStore } from "../store";
import type { TemplateInfo } from "../types";
import { Modal } from "./Modal";

export function templateTitle(t: TemplateInfo) {
  return t.kind === "series" ? `「${t.name}」的默认设置` : `预设「${t.name}」`;
}

export function TemplateDialog({ doc, onClose, onRestoredLastRun }: { doc: ConfigDoc; onClose: () => void; onRestoredLastRun: () => void }) {
  const s = useStore();
  const t = doc.template!;
  const unsaved = useUnsaved();
  const [busy, setBusy] = useState(false);
  const changes = t.changes ?? [];
  const go = async (keys: string[][], to: "episode" | "template") => {
    setBusy(true);
    try { await doc.applyTemplate!(keys, to); s.toast(to === "episode" ? "已恢复成模板的值（原来的设置备份在项目的 backup 文件夹）" : `已存到${templateTitle(t)}（原来的已备份）`); }
    finally { setBusy(false); }
  };
  const noEdit = t.editable ? "" : "内置预设不能改：先在「设置 › 预设」里复制成自己的";
  return (
    <Modal onClose={onClose} width={820}>
      <div className="row" style={{ marginBottom: 6 }}>
        <h3 style={{ margin: 0 }}>和模板不同的 {changes.length} 项</h3><span className="chip tpl">模板：{templateTitle(t)}</span>
        <span className="sp" /><button className="btn sm ghost" onClick={onClose}>关闭</button>
      </div>
      <div className="hint" style={{ marginBottom: 10 }}>
        逐项决定用哪边的；被改掉的那份先备份。所属作品、外挂字幕文件、节目说明只属于这一集，不列出。
        {t.kind === "preset" ? "这一集不属于有默认设置的作品，模板是建它时选的预设。" : "改作品默认设置不会影响已有的集，除非存过去以后再同步。"}
      </div>
      {unsaved && <div className="banner">还有设置正在保存，等一下再操作。</div>}
      <div style={{ maxHeight: "56vh", overflow: "auto" }}>
        {changes.length === 0 ? <div className="dim" style={{ padding: 10 }}>和模板完全一样。</div> : (
          <table className="tb"><thead><tr><th>设置项</th><th>这一集</th><th>模板</th><th /></tr></thead><tbody>
            {changes.map((c) => (
              <tr key={c.path}>
                <td>{settingLabel(c.keys)}{c.path === "render.profiles" && <div className="hint">字幕文件整份比较：图层、样式、排版有一处不同就算不同，在「输出」里看</div>}
                  {c.keys[1] === "styles" && c.keys.length === 3 && <div className="hint">{c.before == null ? "这一集没有这个样式" : c.after == null ? "模板里没有这个样式" : ""}</div>}</td>
                <td title={JSON.stringify(c.before)}><b style={{ fontWeight: 500 }}>{showVal(c.before)}</b></td><td className="muted" title={JSON.stringify(c.after)}>{showVal(c.after)}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  <button className="btn sm" disabled={busy || unsaved} onClick={() => go([c.keys], "episode")}>↺ 恢复成模板</button>{" "}
                  <button className="btn sm tplb" disabled={busy || unsaved || !t.editable} title={noEdit} onClick={() => go([c.keys], "template")}>存到模板</button>
                </td>
              </tr>
            ))}
          </tbody></table>)}
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        {t.last_run && <span className="link" onClick={async () => {
          if (!s.pid || !confirm("这一集的设置换回上次运行时的样子？现在的设置会先备份。")) return;
          try { await api.restoreLastRun(s.pid); onRestoredLastRun(); s.toast("已换回上次运行时的设置"); } catch (e) { s.setError((e as Error).message); }
        }}>回到上次运行时的设置…</span>}
        <span className="sp" />
        <button className="btn" disabled={busy || unsaved || !changes.length} onClick={() => go(changes.map((c) => c.keys), "episode")}>全部恢复成模板</button>
        <button className="btn tplb" disabled={busy || unsaved || !changes.length || !t.editable} title={noEdit} onClick={() => go(changes.map((c) => c.keys), "template")}>全部存到模板</button>
      </div>
    </Modal>
  );
}

export function SavePresetDialog({ onClose }: { onClose: () => void }) {
  const s = useStore();
  const [name, setName] = useState((s.summary?.name ?? "").replace(/\.[^.]+$/, "") + " 风格");
  const [inc, setInc] = useState({ process: true, output: true, langs: false });
  const [busy, setBusy] = useState(false);
  return (
    <Modal onClose={onClose}>
      <h3>把这一集的设置存成预设</h3>
      <div className="kv">
        <span className="lbl">名字</span><input className="inp" value={name} onChange={(e) => setName(e.target.value)} />
        <span className="lbl">包括</span>
        <div className="col" style={{ gap: 3 }}>
          <label className="row"><input type="checkbox" checked={inc.process} onChange={(e) => setInc({ ...inc, process: e.target.checked })} />识别、翻译的设置</label>
          <label className="row"><input type="checkbox" checked={inc.output} onChange={(e) => setInc({ ...inc, output: e.target.checked })} />字幕文件、样式、输出视频</label>
          <label className="row"><input type="checkbox" checked={inc.langs} onChange={(e) => setInc({ ...inc, langs: e.target.checked })} />语言</label>
        </div>
      </div>
      <div className="hint" style={{ marginTop: 8 }}>所属作品、外挂字幕文件、节目说明不会存进去。存好后在「设置 › 预设」里管理，新建项目时可以选。</div>
      <div className="row" style={{ marginTop: 12, justifyContent: "flex-end" }}>
        <button className="btn" onClick={onClose}>取消</button>
        <button className="btn pri" disabled={busy || !name.trim()} onClick={async () => {
          setBusy(true);
          try { await api.createPreset({ name: name.trim(), from_pid: s.pid!, include: inc }); s.toast(`已存成预设「${name.trim()}」`); onClose(); }
          catch (e) { s.setError((e as Error).message); } finally { setBusy(false); }
        }}>存成预设</button>
      </div>
    </Modal>
  );
}

/** 作品默认设置这次改了的几项，同步给已有的集。自己单独改过那一项的集默认不勾。 */
export function SyncDialog({ series, keys, before, after, onClose }: {
  series: string; keys: string[][]; before: unknown[]; after: unknown[]; onClose: () => void;
}) {
  const s = useStore();
  const [rows, setRows] = useState<{ pid: string; name: string; running: boolean; values: unknown[] }[] | null>(null);
  const [pick, setPick] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.seriesValues(series, keys).then((r) => {
      setRows(r);
      setPick(Object.fromEntries(r.map((x) => [x.pid, !x.running && x.values.every((v, i) => JSON.stringify(v) === JSON.stringify(before[i]))])));
    }).catch((e) => s.setError(e.message));
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const why = (x: { running: boolean; values: unknown[] }) => {
    if (x.running) return "正在运行：跑完后再同步";
    const own = keys.filter((_, i) => JSON.stringify(x.values[i]) !== JSON.stringify(before[i]) && JSON.stringify(x.values[i]) !== JSON.stringify(after[i]));
    const same = keys.every((_, i) => JSON.stringify(x.values[i]) === JSON.stringify(after[i]));
    return same ? "已经是新的值" : own.length ? `自己改过：${own.map(settingLabel).join("、")}` : "还是旧值，可以同步";
  };
  const n = Object.values(pick).filter(Boolean).length;
  return (
    <Modal onClose={onClose} width={720}>
      <h3>把作品默认设置这次的改动同步给已有的集</h3>
      <div className="hint" style={{ marginBottom: 10 }}>
        这次改了：{keys.map((k, i) => `${settingLabel(k)} ${showVal(before[i])} → ${showVal(after[i])}`).join("；")}。只同步这 {keys.length} 项，别的不动；每集同步前先备份。
      </div>
      {!rows ? <div className="dim">加载中…</div> : rows.length === 0 ? <div className="dim">这部作品还没有别的集。</div> : (
        <table className="tb"><thead><tr><th /><th>集</th><th>情况</th></tr></thead><tbody>
          {rows.map((x) => (
            <tr key={x.pid} className={pick[x.pid] ? "" : "off"}>
              <td style={{ width: 30 }}><input type="checkbox" disabled={x.running} checked={!!pick[x.pid]} onChange={(e) => setPick({ ...pick, [x.pid]: e.target.checked })} /></td>
              <td>{x.name}</td><td className="muted">{why(x)}</td>
            </tr>
          ))}
        </tbody></table>)}
      <div className="row" style={{ marginTop: 12, justifyContent: "flex-end" }}>
        <button className="btn" onClick={onClose}>不同步</button>
        <button className="btn tplb" disabled={busy || !n} onClick={async () => {
          setBusy(true);
          try {
            const r = await api.seriesSync(series, keys, Object.keys(pick).filter((k) => pick[k]));
            s.toast(`已同步给 ${r.synced.length} 集${r.skipped.length ? `，${r.skipped.length} 集正在运行没有同步` : ""}`);
            if (s.pid && r.synced.includes(s.pid)) await s.reload();
            onClose();
          } catch (e) { s.setError((e as Error).message); } finally { setBusy(false); }
        }}>同步给 {n} 集</button>
      </div>
    </Modal>
  );
}
