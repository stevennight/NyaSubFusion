// 视频播放 + 字幕叠加。不能直接播放的格式会先在内核里转一个预览文件。
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { Candidate, Preview } from "../types";
import { SubtitleOverlay, useSize } from "./SubtitleOverlay";

export interface PlayerHandle { seek: (t: number) => void; toggle: () => void; playing: boolean }

export const Player = forwardRef<PlayerHandle, {
  preview: Preview | null; boxes?: Candidate[]; showSubs?: boolean; onTime: (t: number) => void; style?: React.CSSProperties;
}>(function Player({ preview, boxes, showSubs = true, onTime, style }, ref) {
  const s = useStore();
  const video = useRef<HTMLVideoElement>(null);
  const [frame, size] = useSize<HTMLDivElement>();
  const [time, setTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [state, setState] = useState<"ok" | "building" | "failed">("ok");
  const [nonce, setNonce] = useState(0);

  useImperativeHandle(ref, () => ({
    seek: (t) => { if (video.current) video.current.currentTime = Math.max(0, t); setTime(t); onTime(t); },
    toggle: () => { const v = video.current; if (v) (v.paused ? v.play() : v.pause()); },
    playing,
  }), [playing, onTime]);

  // 预览视频还没生成时轮询
  useEffect(() => {
    if (!s.pid) return;
    let stop = false;
    const check = async () => {
      const r = await fetch(api.videoUrl(s.pid!), { method: "GET", headers: { Range: "bytes=0-0" } });
      if (stop) return;
      if (r.status === 202) { setState("building"); setTimeout(check, 3000); }
      else if (!r.ok) setState("failed");
      else { setState("ok"); setNonce((n) => n + 1); }
    };
    check();
    return () => { stop = true; };
  }, [s.pid]);

  // 播放时用 requestAnimationFrame 更新时间（timeupdate 太稀疏）
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    const tick = () => {
      const t = video.current?.currentTime ?? 0;
      setTime(t);
      onTime(t);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, onTime]);

  return (
    <div className="frame" ref={frame} style={style}>
      {state === "ok" && s.pid && (
        <video key={nonce} ref={video} src={api.videoUrl(s.pid)} preload="auto"
               onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
               onSeeked={(e) => { const t = e.currentTarget.currentTime; setTime(t); onTime(t); }} />
      )}
      {state === "building" && <div className="wait">正在生成预览视频（浏览器不能直接播放这个格式）…</div>}
      {state === "failed" && <div className="wait">无法播放这个视频</div>}
      {showSubs && <SubtitleOverlay preview={preview} time={time} width={size.w} height={size.h} boxes={boxes} />}
    </div>
  );
});
