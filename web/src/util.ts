import type { CSSProperties } from "react";
import type { Style } from "./types";

export function fmt(t: number) {
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(1).padStart(4, "0")}`;
}

export const TYPE_LBL: Record<string, string> = {
  dialogue: "台词", narration: "旁白", info: "说明", name: "人名条", title: "标题", song: "歌词", sfx: "音效",
};
export const FLAG_LBL: Record<string, string> = {
  asr_only: "仅语音", ocr_low_conf: "OCR 低置信", conflict: "来源冲突", reading_guess: "读音待确认",
  cps_high: "语速过快", llm_fallback: "大模型漏译",
};
export const SRC_LBL: Record<string, string> = { c: "字幕", o: "画面字", a: "语音" };
export const STAGE_LBL: Record<string, string> = {
  probe: "探测媒体", audio: "提取音频", subs: "自带字幕", asr: "语音识别", ocr: "画面字识别", ocr_refine: "VLM 复核",
  align: "字幕对时", fuse: "规则融合", translate: "融合 + 翻译", annotate: "注音", render: "排版输出",
  mux: "封装 MKV", burn: "压制硬字幕",
};

export function pct(p: { done: number; total: number } | null | undefined) {
  return p && p.total > 0 ? Math.min(100, Math.round((p.done / p.total) * 100)) : 0;
}

export function langName(code: string, langs: { code: string; name: string }[]) {
  return langs.find((l) => l.code === code)?.name ?? code;
}

/** 字幕样式 → CSS。k = 画面高度 / 1080。描边用多重阴影模拟。 */
export function styleCss(st: Style, k: number): CSSProperties {
  const w = st.outline * k;
  const shadows: string[] = [];
  if (w > 0) {
    for (let i = 0; i < 16; i++) {
      const a = (i / 16) * Math.PI * 2;
      shadows.push(`${(Math.cos(a) * w).toFixed(2)}px ${(Math.sin(a) * w).toFixed(2)}px 0 ${st.outline_color}`);
    }
  }
  if (st.shadow > 0) shadows.push(`${st.shadow * k}px ${st.shadow * k}px ${st.shadow * k * 1.5}px #000a`);
  return {
    fontFamily: `"${st.font}", "Microsoft YaHei", "Yu Gothic", sans-serif`,
    fontSize: st.size * k,
    color: st.color,
    fontWeight: st.bold ? 700 : 400,
    fontStyle: st.italic ? "italic" : "normal",
    textShadow: shadows.join(","),
  };
}

export function deepGet(obj: Record<string, unknown> | undefined, path: string): unknown {
  return path.split(".").reduce<unknown>((o, k) => (o && typeof o === "object" ? (o as Record<string, unknown>)[k] : undefined), obj);
}

/** 不可变地设置嵌套键：setIn({}, "asr.model", "x") → {asr: {model: "x"}} */
export function setIn<T extends Record<string, unknown>>(obj: T, path: string, value: unknown): T {
  const [head, ...rest] = path.split(".");
  const cur = (obj?.[head] ?? {}) as Record<string, unknown>;
  return { ...obj, [head]: rest.length ? setIn(cur, rest.join("."), value) : value };
}
