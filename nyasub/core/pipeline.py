"""流水线：probe → audio → subs → asr → ocr → ocr_refine → align → fuse → translate → render。

每一步的结果存在 `<stem>.nyasub/stages/<步骤>.json`，旁边的 `.fp` 记录指纹：
    输入指纹 = hash(算法版本 + 相关配置 + 额外输入（文件大小/时间等）+ 上游各步的输出指纹)
    输出指纹 = hash(这一步的结果内容)
输入指纹没变就直接复用；上游重跑但结果内容没变时，下游也不用重跑。
"""
import os
import time
from dataclasses import dataclass, field

from .. import lang
from ..env import log
from . import config as config_mod
from .store import ProjectDir, digest, file_stamp, load_json, save_json

STAGES = ["probe", "audio", "subs", "asr", "ocr", "ocr_refine", "align", "fuse", "translate", "render"]
ALIASES = {"merge": "fuse", "extract": "audio"}
VIDEO_EXTS = (".mp4", ".mkv", ".ts", ".m2ts", ".mts", ".avi", ".mov", ".webm", ".flv", ".wmv", ".mpg", ".mpeg")
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus")


@dataclass
class Options:
    until: str = "render"
    redo: set = field(default_factory=set)
    subs_path: str | None = None     # None = 按配置；"none" = 不用字幕
    translate: str | None = None     # 覆盖 llm.mode
    config_path: str | None = None
    workdir: str | None = None


class Waiting(Exception):
    """翻译任务已导出，等外部助手完成。"""


@dataclass
class StageDef:
    deps: list
    keys: list
    version: int = 1


DEFS = {
    "probe": StageDef([], []),
    "audio": StageDef(["probe"], []),
    "subs": StageDef(["probe"], ["subs.enabled", "subs.ruby_size_ratio", "subs.track"]),
    "asr": StageDef(["audio"], ["asr.enabled", "asr.model", "asr.beam_size", "asr.vad", "asr.vad_threshold",
                                "project.source"]),
    "ocr": StageDef(["probe"], ["ocr.enabled", "ocr.fps", "ocr.height", "ocr.det_model", "ocr.rec_model",
                                "ocr.min_score", "ocr.min_duration", "ocr.max_gap", "ocr.persistent_seconds",
                                "ocr.regions", "ocr.skip_static"]),
    "ocr_refine": StageDef(["ocr"], ["ocr.refine", "llm.vision_model", "llm.model"]),
    "align": StageDef(["subs", "asr"], ["fusion.auto_sync"]),
    "fuse": StageDef(["align", "ocr_refine", "asr"], ["fusion"]),
    "translate": StageDef(["fuse"], ["project.targets", "project.context", "llm.model", "llm.temperature",
                                     "llm.chunk_seconds", "llm.chunk_max_items", "render.lang_options"], version=1),
}


def collect_inputs(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(os.path.join(p, n) for n in os.listdir(p) if n.lower().endswith(VIDEO_EXTS + AUDIO_EXTS))
        elif os.path.isfile(p):
            out.append(p)
        else:
            log(f"找不到：{p}")
    return out


class Run:
    def __init__(self, video, cfg, opts: Options, config_files=()):
        self.cfg = cfg
        self.opts = opts
        self.proj = ProjectDir(video, opts.workdir)
        self.results = {}
        self.fps = {}           # 步骤 → {"in", "out"}
        self.ran = []           # 本次实际重跑的步骤
        self.src = None         # 解析后的源语言
        self.config_files = list(config_files)

    # ---------- 指纹 ----------
    def _fp_path(self, name):
        return self.proj.stage(name + ".fp")

    def _out_path(self, name):
        return self.proj.stage(name + ".json")

    def _extra(self, name):
        if name == "probe":
            return file_stamp(self.proj.video)
        if name == "subs":
            path = self._subs_file()
            return {"arg": self.opts.subs_path, "file": file_stamp(path) if path else None}
        if name == "ocr":
            return {"lang": lang.get(self.src).ocr_lang if self.src else None}
        if name == "fuse":
            return {"src": self.src}
        if name == "translate":
            mode = self._llm_mode()
            jobs = None
            if mode == "export":  # 外部助手改了结果文件，就重新导入
                jobs = [file_stamp(f)["mtime"] for f in sorted((self.proj / "jobs").glob("*.result.json"))]
            return {"src": self.src, "mode": mode, "series": self._series_terms_digest(), "jobs": jobs}
        return None

    def input_fp(self, name):
        d = DEFS[name]
        return digest({"v": d.version, "cfg": self.cfg.pick(*d.keys), "extra": self._extra(name),
                       "deps": [self.fps.get(x, {}).get("out") for x in d.deps]})

    def _cached(self, name, fp_in):
        rec = load_json(self._fp_path(name))
        if name in self.opts.redo or "all" in self.opts.redo or not rec or rec.get("in") != fp_in:
            return None
        if not self._out_path(name).exists():
            return None
        return rec

    def stage(self, name, fn, force=False):
        fp_in = self.input_fp(name)
        rec = None if force else self._cached(name, fp_in)
        if rec:
            self.results[name] = load_json(self._out_path(name))
            self.fps[name] = rec
            return self.results[name]
        t = time.time()
        result = fn()
        save_json(self._out_path(name), result)
        rec = {"in": fp_in, "out": digest(result), "seconds": round(time.time() - t, 1)}
        save_json(self._fp_path(name), rec)
        self.results[name] = result
        self.fps[name] = rec
        self.ran.append(name)
        return result

    def _stop(self, name):
        return STAGES.index(name) >= STAGES.index(self.opts.until)

    # ---------- 主流程 ----------
    def run(self):
        cfg, p = self.cfg, self.proj
        log(f"=== {p.video.name}")
        log(f"工作目录：{p.root}")
        t_all = time.time()
        from ..sources import media

        info = self.stage("probe", lambda: media.probe(str(p.video)))
        if self._stop("probe"):
            return self._done(t_all)

        def do_audio():
            if not info.get("has_audio"):
                return {"wav": None}
            log("提取音频")
            media.extract_audio(str(p.video), str(p.media("audio.wav")))
            return {"wav": "media/audio.wav", "stamp": file_stamp(p.media("audio.wav"))["size"]}
        self.stage("audio", do_audio, force=bool(info.get("has_audio")) and not p.media("audio.wav").exists())

        subs = self.stage("subs", lambda: self._load_subs(info))
        if self._stop("subs"):
            return self._done(t_all)

        asr = self.stage("asr", self._run_asr)
        self.src = self._resolve_source(asr, subs)
        log(f"源语言：{lang.get(self.src).name}（{self.src}）")
        if self._stop("asr"):
            return self._done(t_all)

        ocr = self.stage("ocr", lambda: self._run_ocr(info))
        refined = self.stage("ocr_refine", lambda: self._refine(ocr))
        if self._stop("ocr"):
            return self._done(t_all)

        aligned = self.stage("align", lambda: self._align(subs, asr))
        fused = self.stage("fuse", lambda: self._fuse(aligned, refined, asr, info))
        self._log_fuse(fused)
        aspect = (info.get("video") or {}).get("aspect") or 16 / 9
        boxes = self._hardsub_boxes(refined, fused)
        if cfg.render.merged_debug:
            from ..render import ass

            path = f"{p.out_base}.{self.src}-merged.ass"
            ass.write_merged(path, fused["cues"], cfg.render.styles, aspect, boxes)
            log(f"已输出原文合并字幕：{os.path.basename(path)}")
        if self._stop("fuse"):
            return self._done(t_all)

        mode = self._llm_mode()
        if mode == "off":
            log("翻译已关闭（llm.mode = off）")
            return self._done(t_all)
        try:
            translated = self.stage("translate", lambda: self._translate(fused, mode))
        except Waiting:
            return self._done(t_all)
        if self._stop("translate"):
            return self._done(t_all)

        self._render(translated["cues"], aspect, boxes)
        return self._done(t_all)

    def _done(self, t0):
        log(f"完成，用时 {time.time() - t0:.0f}s" + (f"（重做：{', '.join(self.ran)}）" if self.ran else "（全部复用缓存）"))
        return self

    # ---------- 各步骤 ----------
    def _subs_file(self):
        """用户指定或同名的外挂字幕文件（内封字幕不算，它随视频指纹变化）。"""
        arg = self.opts.subs_path or self.cfg.subs.path
        if arg and arg.lower() == "none":
            return None
        if arg:
            return arg
        from ..sources import subtitle

        return subtitle.find_external(str(self.proj.video), skip_names=self.output_names())

    def _load_subs(self, info):
        from ..sources import media, subtitle

        c = self.cfg.subs
        ratio = c.ruby_size_ratio
        if not c.enabled or (self.opts.subs_path or "").lower() == "none":
            log("不使用字幕")
            return []
        path = self._subs_file()
        if path and c.track is None:
            cues = subtitle.load_cues(path, ratio)
            log(f"字幕：{os.path.basename(path)}，{len(cues)} 条")
            return cues
        tracks = [s for s in info["subtitles"] if s["codec"] in media.TEXT_SUB_CODECS]
        if c.track is not None:
            tracks = [s for s in info["subtitles"] if s["sub_index"] == c.track]
        if not tracks and info.get("data_streams") and c.track is None:
            cues = self._arib_ttml(info, ratio)
            if cues:
                return cues
        if not tracks:
            if info["subtitles"]:
                log(f"内封字幕都是图片格式（{', '.join(s['codec'] for s in info['subtitles'])}），暂不支持，按无字幕处理")
            else:
                log("没有找到字幕")
            return []
        want = self.cfg.project.source
        tracks.sort(key=lambda s: (s["codec"] != "arib_caption",
                                   want != "auto" and lang.canonical(s["lang"]) != lang.canonical(want)))
        tr = tracks[0]
        out = self.proj.media(f"embedded_sub_{tr['sub_index']}.ass")
        media.extract_sub_stream(str(self.proj.video), tr, str(out))
        cues = subtitle.load_cues(str(out), ratio)
        log(f"字幕：内封字幕轨 #{tr['sub_index']}（{tr['codec']} {tr['lang']}），{len(cues)} 条")
        return cues

    def _arib_ttml(self, info, ratio):
        from ..sources import media, subtitle

        for idx in info["data_streams"]:
            probe_path = self.proj.media(f"data_{idx}.probe")
            media.dump_data_stream(str(self.proj.video), idx, str(probe_path), probe_bytes=1 << 20)
            head = probe_path.read_bytes()
            probe_path.unlink()
            if not subtitle.is_arib_ttml(head):
                continue
            log(f"发现 4K 广播字幕（ARIB-TTML，数据流 #{idx}），正在完整导出")
            bin_path = self.proj.media(f"arib_ttml_{idx}.bin")
            media.dump_data_stream(str(self.proj.video), idx, str(bin_path))
            cues = subtitle.load_arib_ttml(str(bin_path), ratio)
            if cues:
                log(f"字幕：ARIB-TTML（数据流 #{idx}），{len(cues)} 条")
                return cues
        return []

    def _run_asr(self):
        c = self.cfg.asr
        wav = (self.results.get("audio") or {}).get("wav")
        if not c.enabled or not wav:
            return {"segments": [], "language": None}
        from ..sources import asr

        src = self.cfg.project.source
        whisper_lang = None if src == "auto" else lang.get(src).whisper
        info = self.results["probe"]
        t = time.time()
        segs, detected = asr.transcribe(str(self.proj / wav), c.model_dump(), whisper_lang,
                                        offset=info.get("audio_offset", 0.0), duration=info["duration"])
        log(f"语音识别完成：{len(segs)} 段，用时 {time.time() - t:.0f}s")
        return {"segments": segs, "language": detected}

    def _resolve_source(self, asr, subs):
        src = self.cfg.project.source
        if src != "auto":
            return lang.canonical(src)
        if asr.get("language"):
            return lang.canonical(asr["language"])
        guess = lang.detect("".join(c["text"] for c in subs[:200]))
        if guess:
            return guess
        log("无法判断源语言（没有语音也没有字幕），按日语处理；可在 [project] source 里指定")
        return "ja"

    def _run_ocr(self, info):
        c = self.cfg.ocr
        if not c.enabled or not info.get("video"):
            return []
        from ..sources import ocr

        t = time.time()
        events = ocr.scan(str(self.proj.video), info, c.model_dump(), str(self.proj.root), lang.get(self.src).ocr_lang)
        log(f"画面字识别完成：{len(events)} 条，用时 {time.time() - t:.0f}s")
        return events

    def _refine(self, events):
        events = [dict(e) for e in events]
        if self.cfg.ocr.refine != "vlm" or not events:
            return events
        from ..fusion.fuse import prefilter
        from ..llm.refine import refine_ocr

        prefilter(events, self.results["probe"]["duration"], lang.get(self.src))
        return refine_ocr(events, self.cfg, str(self.proj.root), self.proj / "llm_cache")

    def _align(self, subs, asr):
        from ..fusion.align import apply_offset, estimate_offset

        segs = asr["segments"]
        if not (subs and segs and self.cfg.fusion.auto_sync):
            return {"subs": subs, "offset": 0.0}
        off, gain = estimate_offset(subs, segs)
        if abs(off) >= 0.15 and gain >= 0.05:
            log(f"字幕与语音整体差 {off:+.1f} 秒，已自动对齐（匹配度提升 {gain * 100:.0f}%）")
            return {"subs": apply_offset(subs, off), "offset": off}
        return {"subs": subs, "offset": 0.0}

    def _fuse(self, aligned, ocr_events, asr, info):
        from ..fusion.fuse import fuse

        events = [dict(e) for e in ocr_events]
        out = fuse(aligned["subs"], events, asr["segments"], self.cfg.fusion.model_dump(), lang.get(self.src),
                   info["duration"])
        out["ocr_marks"] = {e["id"]: {k: e[k] for k in ("dropped", "dup_of") if e.get(k)} for e in events}
        return out

    def _log_fuse(self, fused):
        s = fused["stats"]
        log(f"融合：字幕 {s['subs']} 条；画面字 {s['ocr_total']} 条（台标 {s['ocr_logo']}，杂讯 {s['ocr_junk']}，"
            f"实物上的字 {s['ocr_scene']}，与字幕重复 {s['ocr_dup']}，台词 {s['telop']}，说明 {s['info']}）；"
            f"语音 {s['asr_total']} 段（无字幕覆盖 {s['asr_uncovered']}，作为参考附到字幕上 {s['asr_hint']}）")

    def _hardsub_boxes(self, ocr_events, fused):
        """真正的硬字幕框（排除台标、背景文字），渲染时用来让开位置。"""
        keep = {it["id"] for it in fused["items"] if it["src"] in ("telop", "info")}
        marks = fused.get("ocr_marks", {})
        return [{"start": e["start"], "end": e["end"], "rect": e["rect"], "vertical": e.get("vertical")}
                for e in ocr_events if e["id"] in keep or marks.get(e["id"], {}).get("dup_of")]

    # ---------- 翻译 ----------
    def _llm_mode(self):
        return self.opts.translate or self.cfg.llm_mode()

    def _series_terms(self):
        name = self.cfg.project.series
        if not name:
            return []
        return load_json(config_mod.series_dir(name) / "glossary.json", {"terms": []})["terms"]

    def _series_terms_digest(self):
        return digest(self._series_terms()) if self.cfg.project.series else None

    def _translate(self, fused, mode):
        from ..llm import jobs, translate

        items, known = fused["items"], self._series_terms()
        if not items:
            return {"cues": [], "glossary": {"summary": "", "terms": known, "new": 0}}
        if mode == "api":
            cues, glossary = translate.run_api(items, self.cfg, self.proj / "llm_cache", self.src, known)
        else:
            job_dir = self.proj / "jobs"
            cues, missing, glossary = jobs.import_jobs(job_dir, self.cfg, self.src, known)
            stamp = job_dir / "source.fp"
            fp = self.fps["fuse"]["out"]
            stale = not stamp.exists() or stamp.read_text(encoding="utf-8").strip() != fp
            if cues is None or stale:
                job_dir, n = jobs.export_jobs(items, self.cfg, job_dir, str(self.proj.video), self.src, known, fp,
                                              self.opts.config_path)
                log(f"已导出 {n} 段翻译任务：{job_dir}")
                self._hint(job_dir)
                raise Waiting()
            if missing:
                log(f"翻译任务还没完成：还差 {len(missing)} 段（{missing[0]} …）")
                self._hint(job_dir)
                raise Waiting()
            if len(self.cfg.project.targets) > 1:
                log("提示：导出任务模式目前只翻译第一个目标语言")
            log(f"已导入翻译结果：{len(cues)} 条")
        translate.add_quality_flags(cues)
        self._save_series_terms(glossary)
        todo = sum(1 for c in cues if c["review"] == "todo")
        log(f"翻译完成：{len(cues)} 条字幕，{todo} 条标记待审校；术语新增 {glossary.get('new', 0)} 条")
        return {"cues": cues, "glossary": glossary}

    def _save_series_terms(self, glossary):
        name = self.cfg.project.series
        if not name or not glossary.get("new"):
            return
        path = config_mod.series_dir(name) / "glossary.json"
        save_json(path, {"terms": glossary["terms"]})
        log(f"作品「{name}」术语表已更新：共 {len(glossary['terms'])} 条")

    def _hint(self, job_dir):
        log("下一步：在 Claude Code 里对它说（翻译完它会自己生成最终字幕）：")
        log(f"    请按 {job_dir / 'TASK.md'} 完成翻译任务")

    # ---------- 输出 ----------
    def _profile_names(self, profile):
        targets = [lang.canonical(t) for t in self.cfg.project.targets]
        tr = next((lang.canonical(ly.lang or targets[0]) for ly in profile.layers if ly.role == "translation"),
                  self.src or "orig")
        return profile.filename.format(stem=self.proj.stem, tr=tr, src=self.src or "orig", ext=profile.format,
                                       name=profile.name)

    def output_names(self):
        names = []
        for prof in self.cfg.render.profiles:
            try:
                names.append(self._profile_names(prof))
            except (KeyError, IndexError):
                pass
        return names

    def _render(self, cues, aspect, boxes):
        from ..render import ass, layout, srt

        cues = apply_edits(cues, load_json(self.proj / "edits.json", {}))
        targets = {lang.canonical(t) for t in self.cfg.project.targets}
        seen = set()
        for prof in self.cfg.render.profiles:
            if not prof.enabled:
                continue
            missing = {lang.canonical(ly.lang) for ly in prof.layers
                       if ly.role == "translation" and ly.lang and ly.show != "off"} - targets
            if missing:
                log(f"提示：方案「{prof.name}」用到了 {', '.join(sorted(missing))}，但 [project] targets 里没有，这些图层会是空的")
            name = self._profile_names(prof)
            if name in seen:
                name = name.rsplit(".", 1)[0] + f".{prof.name}.{prof.format}"
            seen.add(name)
            items = layout.build(cues, prof, self.cfg, self.src)
            path = self.proj.video.with_name(name)
            if prof.format == "ass":
                ass.write(str(path), items, prof, self.cfg.render.styles, aspect, boxes)
            else:
                srt.write(str(path), items)
            log(f"已输出「{prof.name}」：{name}")


def apply_edits(cues, edits: dict):
    """edits.json：{cue_id: {"start": .., "end": .., "type": .., "orig": "..", "tr": {"zh-Hans": ".."}}}"""
    if not edits:
        return cues
    out = []
    for c in cues:
        e = edits.get(c["id"])
        if not e:
            out.append(c)
            continue
        if e.get("delete"):
            continue
        c = dict(c, tr={k: dict(v) for k, v in c.get("tr", {}).items()}, orig=dict(c["orig"]))
        for k in ("start", "end", "type"):
            if k in e:
                c[k] = e[k]
        if "orig" in e:
            c["orig"]["text"] = e["orig"]
        for code, text in (e.get("tr") or {}).items():
            c["tr"][code] = {"text": text}
        out.append(c)
    return out


def process(video, cfg, opts: Options, config_files=()):
    return Run(video, cfg, opts, config_files).run()
