// 视频播放 + 字幕叠加。浏览器不能直接播放的格式由内核转成 540p 的 HLS 分段：转出第一段就能开始播，边转边看。
import type Hls from "hls.js";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { api, TOKEN } from "../api";
import { useStore } from "../store";
import type { Candidate, MediaInfo, Preview } from "../types";
import { fmt, pct } from "../util";
import { SubtitleOverlay, useSize } from "./SubtitleOverlay";

export interface PlayerHandle { seek: (t: number) => void; toggle: () => void; playing: boolean }

export const Player = forwardRef<PlayerHandle, {
  preview: Preview | null; boxes?: Candidate[]; showSubs?: boolean; onTime: (t: number) => void; style?: React.CSSProperties;
  controls?: boolean;   // 在画面下方显示播放 / 进度条（审校页有自己的时间轴，不用）
}>(function Player({ preview, boxes, showSubs = true, onTime, style, controls = false }, ref) {
  const s = useStore();
  const video = useRef<HTMLVideoElement>(null);
  const hls = useRef<Hls | null>(null);
  const [frame, size] = useSize<HTMLDivElement>();
  const [time, setTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [src, setSrc] = useState<MediaInfo | null>(null);
  const [attached, setAttached] = useState(false);
  const [noHls, setNoHls] = useState(false);
  const duration = s.summary?.meta?.duration ?? 0;

  const seek = (t: number) => {
    const v = video.current;
    // 边转边看：还没转到的地方先跳到已转好的最后
    const limit = src?.kind === "hls" && !src.complete ? Math.max(0, (src.seconds ?? 0) - 1) : Infinity;
    const to = Math.max(0, Math.min(t, limit));
    if (v) v.currentTime = to;
    setTime(to);
    onTime(to);
  };
  useImperativeHandle(ref, () => ({
    seek,
    toggle: () => { const v = video.current; if (v) (v.paused ? v.play() : v.pause()); },
    playing,
  }), [playing, onTime, src]);

  // 问内核怎么播放；预览还在转时每 2 秒更新一次进度
  useEffect(() => {
    if (!s.pid) return;
    let stop = false, timer = 0;
    setSrc(null);
    setAttached(false);
    const check = async () => {
      try {
        const r = await api.mediaSource(s.pid!);
        if (stop) return;
        setSrc(r);
        if (r.kind === "hls" && r.status === "building") timer = window.setTimeout(check, 2000);
      } catch (e) {
        if (!stop) setSrc({ kind: "none", status: "failed", error: (e as Error).message });
      }
    };
    check();
    return () => { stop = true; clearTimeout(timer); };
  }, [s.pid]);

  // HLS：有了第一段就接上 hls.js（event 类型的播放列表，hls.js 会自己刷新，转完自动变成可以随意拖动）
  const canAttach = src?.kind === "hls" && (src.segments ?? 0) > 0;
  useEffect(() => {
    if (!canAttach || attached || !video.current || !s.pid) return;
    setAttached(true);
    const pid = s.pid, el = video.current;
    import("hls.js").then(({ default: H }) => {  // 只有要转换的视频才用到，按需加载
      if (!H.isSupported()) { setNoHls(true); return; }
      const h = new H({
        xhrSetup: (xhr) => { if (TOKEN) xhr.setRequestHeader("x-nyasub-token", TOKEN); },
        liveDurationInfinity: false, startPosition: 0, lowLatencyMode: false,
      });
      h.loadSource(api.hlsUrl(pid));
      h.attachMedia(el);
      hls.current = h;
    });
  }, [canAttach, attached, s.pid]);
  useEffect(() => () => { hls.current?.destroy(); hls.current = null; }, [s.pid]);

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

  const direct = src?.kind === "direct" || src?.kind === "mp4";
  const building = src?.kind === "hls" && src.status === "building";
  const failed = src?.status === "failed" || noHls;
  const ready = direct || canAttach;
  const prog = building && src.total ? { done: src.done ?? 0, total: src.total } : null;

  return (
    <>
      <div className="frame" ref={frame} style={style}>
        {s.pid && (direct || src?.kind === "hls") && (
          <video ref={video} src={direct ? api.videoUrl(s.pid) : undefined} preload="auto"
                 style={{ visibility: ready ? "visible" : "hidden" }}
                 onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
                 onSeeked={(e) => { const t = e.currentTarget.currentTime; setTime(t); onTime(t); }} />
        )}
        {building && !canAttach && (
          <div className="wait">
            <div>正在转换预览视频（这个格式不能直接播放）{prog ? ` ${pct(prog)}%` : "…"}</div>
          </div>
        )}
        {building && canAttach && (
          <div className="chip" style={{ position: "absolute", right: 8, top: 8, background: "#000a", zIndex: 3 }}
               title="预览视频还在转换：可以先看已经转好的部分">
            转换中 {pct(prog)}% · 可播放到 {fmt(src.seconds ?? 0).slice(0, 5)}
          </div>
        )}
        {failed && <div className="wait"><div>无法播放这个视频
          {src?.error && <div className="dim" style={{ fontSize: 11.5, marginTop: 6, maxWidth: 480, wordBreak: "break-all" }}>{src.error}</div>}</div></div>}
        {showSubs && <SubtitleOverlay preview={preview} time={time} width={size.w} height={size.h} boxes={boxes} />}
      </div>
      {controls && (
        <div className="pctl" style={{ width: "100%" }}>
          <button className="iconbtn big" title="播放 / 暂停（空格）" disabled={!ready}
                  onClick={() => { const v = video.current; if (v) (v.paused ? v.play() : v.pause()); }}>{playing ? "❚❚" : "▶"}</button>
          <div className="seekbar">
            {building && duration > 0 && <div className="avail" style={{ width: `${Math.min(100, ((src.seconds ?? 0) / duration) * 100)}%` }} />}
            <input type="range" min={0} max={Math.max(1, duration)} step={0.1} value={time} disabled={!ready}
                   onChange={(e) => seek(Number(e.target.value))} />
          </div>
          <span className="time">{fmt(time)} / {fmt(duration)}</span>
        </div>
      )}
    </>
  );
});
