// 与内核接口对应的数据类型（只写界面用到的字段）

export type StageStatus = "done" | "stale" | "missing" | "running" | "cached" | "failed" | "waiting";

export interface PlanItem { stage: string; status: StageStatus; seconds?: number | null }

export interface Job { status: "running" | "done" | "failed" | "waiting"; error?: string | null; started: number }

export interface ProjectSummary {
  id: string; video: string; name: string; workdir: string;
  meta: { duration: number; video?: { width: number; height: number; aspect: number; codec?: string } | null } | null;
  src: string | null; plan: PlanItem[]; job: Job | null; config_files: string[]; outputs: string[];
  proxy: "direct" | "ready" | "building" | "failed" | "none";
}

export interface ProjectEntry { id: string; video: string; name: string; exists: boolean; opened: number }

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
}

export type Seg = [string] | [string, string] | [string, string, string];
export interface Word { t: string; segs: Seg[]; pos: string; p: boolean; num: boolean; src: string; guess: boolean }
export interface Tokens { orig: Word[] | null; tr: Record<string, Word[]> }

export interface ProjectData {
  meta: ProjectSummary["meta"]; src: string; targets: string[]; aspect: number;
  hardsub: { start: number; end: number; rect: number[] }[];
  subs: Candidate[]; ocr: Candidate[]; asr: Candidate[];
  fused: { items: FusedItem[]; stats: Record<string, number> | null };
  cues: Cue[]; tokens: Record<string, Tokens>;
  glossary: { summary: string; terms: { src: string; reading?: string; tr: string; type?: string }[] } | null;
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

export interface LiveEvent { type: string; name?: string; status?: string; msg?: string; seconds?: number; error?: string; t?: number; job?: Job | null }
