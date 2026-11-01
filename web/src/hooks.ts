import { useEffect, useState } from "react";
import { api } from "./api";
import { useStore } from "./store";
import type { Preview } from "./types";

/** 某个输出方案的预览排版；数据或配置变了自动刷新。 */
export function usePreview(profile: string | undefined) {
  const s = useStore();
  const [preview, setPreview] = useState<Preview | null>(null);
  useEffect(() => {
    if (!s.pid) return;
    let stop = false;
    api.preview(s.pid, profile).then((p) => { if (!stop) setPreview(p); }).catch((e) => s.setError(e.message));
    return () => { stop = true; };
  }, [s.pid, profile, s.version]);
  return preview;
}

export function usePeaks() {
  const s = useStore();
  const [peaks, setPeaks] = useState<{ per_second: number; peaks: number[] } | null>(null);
  // 波形只跟音频有关：换一集、运行开始 / 结束（可能刚提取了音频）、还没有波形时才重读；改字幕不用重读（几百 KB）
  const again = peaks ? 0 : s.version;
  useEffect(() => { if (s.pid) api.peaks(s.pid).then(setPeaks).catch(() => setPeaks(null)); }, [s.pid, s.running, again]);
  return peaks;
}
