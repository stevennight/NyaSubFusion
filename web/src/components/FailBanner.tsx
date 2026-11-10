// 一集页头下面的失败提示：上次的任务在哪一步失败、为什么、该怎么办；能看日志、重新运行、先关掉。
import { useState } from "react";
import { AllLogs } from "../pages/Process";
import { useStore } from "../store";
import { briefError, STAGE_LBL } from "../util";
import { KIND_LBL } from "./RunBar";

/** 常见原因换成一句人话 + 该怎么办；认不出的返回 null（只显示接口原话） */
function advice(err: string) {
  if (/\b402\b|余额|credits/i.test(err)) return { why: "接口账户余额不足", todo: "到接口网站充值后点“重新运行”。" };
  if (/\b401\b|API Key|Unauthorized/i.test(err)) return { why: "API Key 不对或已失效", todo: "到「设置」里检查接口的 API Key，改好后重新运行。" };
  if (/\b429\b|rate limit/i.test(err)) return { why: "接口请求太频繁，被限流了", todo: "等几分钟再重新运行。" };
  if (/timed? ?out|超时|Connect|网络|TransportError/i.test(err)) return { why: "连不上接口（网络问题或接口暂时不可用）", todo: "检查网络后重新运行。" };
  if (/拒绝访问|WinError 5|Permission/i.test(err)) return { why: "文件被占用或没有写入权限", todo: "关掉正在播放 / 打开这个文件的程序后重新运行。" };
  return null;
}

export function FailBanner() {
  const s = useStore();
  const f = s.summary?.failed;
  const [hidden, setHidden] = useState<string | null>(null);   // 关掉的是哪一次失败（下次再失败还会显示）
  const [logs, setLogs] = useState(false);
  if (!f || s.running || s.queued || hidden === f.item) return null;
  const err = f.error ?? "";
  const a = advice(err);
  const step = f.stage ? STAGE_LBL[f.stage] ?? f.stage : "";
  const kind = KIND_LBL[f.kind] ?? "运行";
  const when = f.finished ? new Date(f.finished * 1000).toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
  return (
    <div className="fail-banner">
      <span className="fb-icon">!</span>
      <div className="fb-text">
        <div className="fb-title">上次{kind}{step ? <>在 <b>{step}</b> 这一步</> : ""}失败了{a ? `：${a.why}` : ""}{when && <span className="fb-when">{when}</span>}</div>
        <div className="fb-msg" title={err}>{a ? <>{a.todo}已经完成的步骤会保留，重新运行从{step || "失败的地方"}接着做。</> : briefError(err) || "原因不明"}</div>
        {a && <div className="fb-raw" title={err}>接口原话：{briefError(err)}</div>}
      </div>
      <div className="fb-act">
        <button className="btn sm" onClick={() => setLogs(true)}>查看日志</button>
        {f.kind === "run" && <button className="btn sm pri" onClick={() => s.run({ until: s.runOpts.until, redo: [] })}>重新运行</button>}
        <button className="btn sm ghost" title="先不显示（下次失败还会提示）" onClick={() => setHidden(f.item)}>✕</button>
      </div>
      {logs && <AllLogs onClose={() => setLogs(false)} />}
    </div>
  );
}
