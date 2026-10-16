import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { ProjectEntry } from "../types";

export function ProjectsPage({ onOpened }: { onOpened: () => void }) {
  const s = useStore();
  const [path, setPath] = useState("");
  const [recent, setRecent] = useState<ProjectEntry[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => { api.projects().then(setRecent).catch((e) => s.setError(e.message)); }, [s]);

  const open = async (p: string) => {
    setBusy(true);
    try {
      const sum = await api.open(p.trim().replace(/^"|"$/g, ""));
      s.open(sum.id);
      onOpened();
    } catch (e) {
      s.setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page"><div className="wide">
      <h2>打开项目</h2>
      <div className="drop-zone">
        <strong style={{ color: "var(--tx)", fontSize: 15 }}>输入视频 / 音频文件路径</strong>
        <div className="dim" style={{ margin: "4px 0 12px" }}>mp4 · mkv · ts · m2ts · mov · webm · mp3 · wav · flac（桌面版会支持拖入和文件对话框）</div>
        <div className="row" style={{ maxWidth: 760, margin: "0 auto" }}>
          <input className="inp" placeholder="例如 D:\Videos\海辺の暮らし_第3集.ts" value={path} onChange={(e) => setPath(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && path) open(path); }} />
          <button className="btn pri" disabled={!path || busy} onClick={() => open(path)}>{busy ? "打开中…" : "打开"}</button>
        </div>
      </div>
      <div className="gap" />
      <div className="card">
        <h3>最近项目</h3>
        {recent.length === 0 ? <div className="dim">还没有项目</div> : (
          <table className="tb"><thead><tr><th>文件</th><th>位置</th><th>打开时间</th><th /></tr></thead><tbody>
            {recent.map((p) => (
              <tr key={p.id}>
                <td>{p.name}{!p.exists && <span className="chip bad" style={{ marginLeft: 6 }}>文件不存在</span>}</td>
                <td className="dim" style={{ wordBreak: "break-all" }}>{p.video}</td>
                <td className="dim">{new Date(p.opened * 1000).toLocaleString()}</td>
                <td><button className="btn sm" disabled={!p.exists} onClick={() => open(p.video)}>{s.pid === p.id ? "当前" : "打开"}</button></td>
              </tr>
            ))}
          </tbody></table>
        )}
      </div>
    </div></div>
  );
}
