// 设置项的中文名（和模板比较、同步时列出来用）和它属于哪一步。
import type { Cfg } from "./types";

const LBL: Record<string, string> = {
  "project.source": "语言 › 源语言", "project.targets": "语言 › 翻译成",
  "subs.enabled": "读取字幕 › 开关", "subs.ruby_size_ratio": "读取字幕 › 振假名小字比例",
  "asr.enabled": "语音识别 › 开关", "asr.backend": "语音识别 › 识别方式", "asr.model": "语音识别 › 模型", "asr.separate": "语音识别 › 人声分离",
  "asr.vad_threshold": "语音识别 › 人声门槛", "asr.api_provider": "语音识别 › 云端接口", "asr.api_kind": "语音识别 › 接口类型",
  "asr.api_model": "语音识别 › 云端模型", "asr.api_chunk_seconds": "语音识别 › 每块时长",
  "ocr.enabled": "画面字识别 › 开关", "ocr.fps": "画面字识别 › 每秒看几帧", "ocr.regions": "画面字识别 › 识别区域",
  "ocr.refine": "画面字复核 › 开关", "ocr.refine_mode": "画面字复核 › 复核方式", "ocr.refine_frame_width": "画面字复核 › 整帧画面宽度",
  "ocr.refine_boxed": "画面字复核 › 框出要转写的字", "ocr.refine_batch": "画面字复核 › 每批几处",
  "ocr.refine_context": "画面字复核 › 附整帧画面", "ocr.refine_context_width": "画面字复核 › 附的整帧画面宽度",
  "llm.mode": "翻译 › 方式", "llm.translate": "翻译 › 接口", "llm.frames": "翻译 › 参考画面",
  "llm.grouped": "翻译 › 分组按顺序翻", "llm.reasoning_effort": "翻译 › 思考程度",
  "render.profiles": "输出 › 字幕文件（整份）", "render.strip_marks": "输出 › 文字清理", "render.fallback_style": "输出 › 兜底样式",
  "render.broadcast": "输出 › 广播字幕原样", "render.broadcast_font": "输出 › 广播字幕原样的字体",
  "render.video.mode": "输出 › 运行完自动生成视频", "render.video.profiles": "封装 MKV › 放进哪些字幕", "render.video.default": "封装 MKV › 默认轨",
  "render.video.fonts": "封装 MKV › 字体", "render.video.keep_subs": "封装 MKV › 保留原有字幕轨", "render.video.broadcast": "封装 MKV › 放进广播字幕原样",
  "render.video.burn": "压制硬字幕 › 压哪一个", "render.video.encoder": "压制硬字幕 › 编码", "render.video.quality": "压制硬字幕 › 画质",
  "render.video.height": "压制硬字幕 › 尺寸",
};
const STYLE_FIELD: Record<string, string> = {
  font: "字体", size: "字号", color: "文字颜色", outline_color: "描边颜色", outline: "描边", shadow: "阴影", bold: "粗体", italic: "斜体",
  spacing: "字间距", box: "底色框", box_color: "底色框颜色", box_opacity: "底色框不透明度", base: "来自",
};
const SECTION: Record<string, string> = { subs: "读取字幕", asr: "语音识别", ocr: "画面字识别", fusion: "合并来源", llm: "翻译", render: "输出", project: "语言" };

export function settingLabel(keys: string[]): string {
  const path = keys.join(".");
  if (LBL[path]) return LBL[path];
  if (keys[0] === "render" && keys[1] === "styles" && keys[2]) return `样式「${keys[2]}」${keys[3] ? ` › ${STYLE_FIELD[keys[3]] ?? keys[3]}` : ""}`;
  return `${SECTION[keys[0]] ?? keys[0]} › ${keys.slice(1).join(".")}`;
}

/** 这一项在「处理」的哪一步（输出页的项返回 "output"） */
export function stepOf(keys: string[]): string {
  const [a, b] = keys;
  if (a === "project") return "basic";
  if (a === "subs") return "subs";
  if (a === "asr") return "asr";
  if (a === "ocr") return b?.startsWith("refine") ? "ocr_refine" : "ocr";
  if (a === "fusion") return "fuse";
  if (a === "llm") return "translate";
  return "output";
}

export function showVal(v: unknown): string {
  if (v === undefined || v === null || v === "") return "（空）";
  if (v === true) return "开";
  if (v === false) return "关";
  // 字幕文件列表：写名字；一整个样式：写字体字号
  if (Array.isArray(v) && v.every((x) => x && typeof x === "object" && "name" in x)) return v.map((x) => (x as { name: string }).name).join("、") + "（看下面说明）";
  if (v && typeof v === "object" && "font" in v && "size" in v) return `${(v as { font: string }).font} ${(v as { size: number }).size}`;
  const t = typeof v === "string" ? v : JSON.stringify(v);
  return t.length > 60 ? t.slice(0, 60) + "…" : t;
}

/** 两份设置哪些项不一样（和内核的 diff_settings 一样：列表整体比较）。 */
export function diffKeys(a: Cfg | undefined, b: Cfg | undefined, prefix: string[] = []): string[][] {
  const out: string[][] = [];
  const ks = new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})]);
  for (const k of [...ks].sort()) {
    const x = a?.[k], y = b?.[k];
    const isObj = (o: unknown) => !!o && typeof o === "object" && !Array.isArray(o);
    if (isObj(x) && isObj(y)) out.push(...diffKeys(x, y, [...prefix, k]));
    else if (JSON.stringify(x) !== JSON.stringify(y)) out.push([...prefix, k]);
  }
  return out;
}

export function getKeys(d: unknown, keys: string[]): unknown {
  return keys.reduce<unknown>((o, k) => (o && typeof o === "object" ? (o as Cfg)[k] : undefined), d);
}
