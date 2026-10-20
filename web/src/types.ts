// 与内核接口对应的数据类型（只写界面用到的字段）

export type StageStatus = "done" | "stale" | "missing" | "running" | "cached" | "failed" | "waiting" | "cancelled" | "off" | "empty";

export interface PlanItem { stage: string; status: StageStatus; seconds?: number | null; note?: string }

export interface Job { status: "running" | "done" | "failed" | "waiting" | "cancelled"; error?: string | null; started: number }

export interface ProjectSummary {
  id: string; video: string; name: string; workdir: string;
  meta: { duration: number; video?: { width: number; height: number; aspect: number; codec?: string } | null } | null;
  src: string | null; plan: PlanItem[]; job: Job | null; config_files: string[]; outputs: string[];
  videos?: { mux?: string; burn?: string };
  render?: { status: StageStatus; note?: string; files: string[] };   // 输出字幕文件这一步
  proxy: "direct" | "ready" | "building" | "failed" | "none";
  configured?: boolean;
  probe?: {
    error?: string; duration?: number; has_audio?: boolean; arib_ttml?: boolean; external_subs?: string | null;
    video?: { width: number; height: number; codec?: string } | null;
    subtitles?: { sub_index: number; codec: string; lang: string; title: string }[];
  };
}

export interface ProjectEntry {
  id: string; video: string; name: string; exists: boolean; opened: number;
  folder?: string; duration?: number | null; source?: string | null; targets?: string[] | null; series?: string;
  reached?: string | null; outputs?: string[]; running?: boolean; configured?: boolean;
}

export interface Candidate {
  id: string; start: number; end: number; text: string;
  rect?: [number, number, number, number]; score?: number; zone?: string; crop?: string;
  dropped?: string; dup_of?: string[];
}

export interface FusedItem extends Candidate { src: "subs" | "telop" | "info" | "asr"; asr_hint?: string; ocr_score?: number }

export interface Cue {
  id: string; start: number; end: number; type: string; sources: string[]; onscreen: boolean;
  rect?: [number, number, number, number];
  orig: { lang: string; text: string };
  tr: Record<string, { text: string }>;
  flags: string[]; review: "ok" | "todo";
  parts?: [number, number][];  // 用“｜”分段时每段的时间（输出时每段单独一屏）
}

export type Seg = [string] | [string, string] | [string, string, string];
export interface Word { t: string; segs: Seg[]; pos: string; p: boolean; num: boolean; src: string; guess: boolean }
export interface Tokens { orig: Word[] | null; tr: Record<string, Word[]> }

export interface EditResult { cue: Cue | null; history: { undo: number; redo: number } }

export interface ProjectData {
  meta: ProjectSummary["meta"]; src: string; targets: string[]; aspect: number;
  hardsub: { start: number; end: number; rect: number[] }[];
  subs: Candidate[]; ocr: Candidate[]; asr: Candidate[];
  fused: { items: FusedItem[]; stats: Record<string, number> | null };
  cues: Cue[]; tokens: Record<string, Tokens>;
  glossary: { summary: string; terms: { src: string; reading?: string; tr: string; type?: string }[] } | null;
  history: { undo: number; redo: number };
}

export interface Style {
  font: string; size: number; color: string; outline_color: string; outline: number; shadow: number; bold: boolean; italic: boolean;
}

export interface PreviewRow { style: string; role: string; chunks: [string, string | null][]; rsize: number; rcolor: string; height: number }
export interface PreviewItem {
  kind: "main" | "info"; start: number; end: number; sources: string[];
  margin_v?: number; rows?: PreviewRow[]; type?: string;
  style?: string; lines?: string[]; top?: boolean; an?: number; x?: number; y?: number;
}
export interface Preview { profile: string | null; profiles: string[]; px: number; py: number; styles: Record<string, Style>; items: PreviewItem[] }

export interface LangInfo { code: string; name: string; annotations: string[]; translit: string | null; line_chars: number; word_based: boolean }

// 配置（只列界面编辑的部分；其余字段原样保留）
export interface Annotate {
  kind: "none" | "furigana" | "pinyin" | "zhuyin"; scope: "all" | "difficult" | "proper" | "first";
  easy_kanji: "grade1" | "grade2"; known_words: string[]; numbers: boolean;
  script: "hiragana" | "katakana" | "romaji"; tone: "mark" | "number" | "none"; size: number; color: string; srt: "paren" | "none";
}
export interface Layer {
  role: "original" | "translation" | "transliteration"; lang: string; of: string; show: "on" | "off" | "auto";
  style: string; max_chars: number; annotate: Annotate; tone: "mark" | "number" | "none";
}
export interface Profile {
  name: string; enabled: boolean; format: "ass" | "srt"; filename: string; layers: Layer[];
  info_mode: "near" | "top" | "off"; info_style: string; avoid_hardsub: boolean; max_lines: number; min_duration: number;
}
export type Cfg = Record<string, any>;

export interface LiveEvent {
  type: string; name?: string; status?: string; msg?: string; seconds?: number; error?: string; t?: number; job?: Job | null;
  stage?: string; done?: number; total?: number; label?: string;   // type = "progress"：某一步的进度；type = "log"：stage 是写日志的步骤
}
export interface Progress { stage: string; done: number; total: number; label?: string }
export interface LogLine { msg: string; stage?: string }

export interface ComponentStatus {
  gpu: { cuda_devices: number; name: string | null; ort_cuda: boolean };
  components: { name: string; label: string; detail: string; installed: boolean; files: number; size_mb: number | null }[];
  job: Job | null;
}

// 桌面壳（Tauri）注入的全局对象；在浏览器里运行时不存在
export interface TauriGlobal {
  dialog: { open: (o: { multiple?: boolean; directory?: boolean; filters?: { name: string; extensions: string[] }[] }) => Promise<string | string[] | null> };
  event: { listen: (name: string, cb: (e: { payload: { paths?: string[] } }) => void) => Promise<() => void> };
}

export interface Term { src: string; reading?: string; tr?: string; type?: string; note?: string; from?: string }
export interface Glossary { series: string; context: string; summary: string; path: string; episode: string; terms: Term[] }
export interface Reading { lang: string; src: string; reading: string; proper?: boolean }

export interface Provider { name: string; base_url: string; model: string; vision_model?: string; api_key_env?: string }
export interface Settings {
  user_config: Cfg; effective: Cfg; path: string; keyring: string;
  keys: Record<string, boolean>; env_keys: Record<string, boolean>;
  translate: { name: string; base_url: string; model: string };
}

export interface QueueOptions { preset?: string; source?: string; targets?: string[]; series?: string; until?: string; video?: "" | "off" | "mux" | "burn" }
export interface QueueItem {
  id: string; pid: string; video: string; name: string; options: QueueOptions;
  status: "pending" | "running" | "done" | "failed" | "waiting" | "cancelled";
  stage: string | null; error: string | null; note: string | null; added: number; started: number | null; finished: number | null;
}
export interface QueueState { paused: boolean; items: QueueItem[]; running: string | null }

/** 界面怎么播放这个视频：direct 原视频 / mp4 旧版预览文件 / hls 边转边看 */
export interface MediaInfo {
  kind: "direct" | "mp4" | "hls" | "none";
  status?: "building" | "ready" | "failed"; error?: string;
  segments?: number; seconds?: number; complete?: boolean; done?: number; total?: number;
}
