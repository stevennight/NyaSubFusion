"""流水线：probe → audio → subs → asr → ocr → ocr_refine → align → fuse → translate → annotate → render。

每一步的结果存在 `<stem>.nyasub/stages/<步骤>.json`，旁边的 `.fp` 记录指纹：
    输入指纹 = hash(算法版本 + 相关配置 + 额外输入（文件大小/时间等）+ 上游各步的输出指纹)
    输出指纹 = hash(这一步的结果内容)
输入指纹没变就直接复用；上游重跑但结果内容没变时，下游也不用重跑。
"""
import os
import time
from dataclasses import dataclass, field

from .. import lang
from ..env import emit, log
from . import config as config_mod
from .store import ProjectDir, digest, file_stamp, load_json, save_json
from .edits import normalize as normalize_edits

STAGES = ["probe", "audio", "subs", "asr", "ocr", "ocr_refine", "align", "fuse", "translate", "annotate", "render"]
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


class Cancelled(Exception):
    """用户取消：在两个步骤之间停下（正在跑的步骤会先做完）。"""


@dataclass
class StageDef:
    deps: list
    keys: list
    version: int = 1


DEFS = {
    "probe": StageDef([], [], version=2),  # v2：记录视频编码（判断能否直接在界面里播放）
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
    # 套用手动修改，再给原文和各译文注音（振假名、拼音…）
    "annotate": StageDef(["translate"], [], version=1),
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
        self.waiting = False    # 翻译任务已导出、等外部助手完成
        self.cancel = None      # threading.Event：设置后在下一个步骤开始前停下
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
        if name == "ocr":  # 识别语言和实际用的模型（auto 时取决于有没有 GPU）都会影响结果
            models = None
            if self.cfg.ocr.det_model == "auto" or self.cfg.ocr.rec_model == "auto":
                from ..sources.ocr import resolve

                r = resolve(self.cfg.ocr.model_dump())
                models = [r["det_model"], r["rec_model"]]
            return {"lang": lang.get(self.src).ocr_lang if self.src else None, "models": models}
        if name == "annotate":
            return {"src": self.src, "edits": digest(load_json(self.proj / "edits.json", {})),
                    "readings": digest(self._user_readings())}
        if name == "fuse":
            return {"src": self.src}
        if name == "ocr_refine":
            return {"model": self.cfg.endpoint("vision")["model"] if self.cfg.ocr.refine == "vlm" else None}
        if name == "translate":
            mode = self._llm_mode()
            jobs = None
            if mode == "export":  # 外部助手改了结果文件，就重新导入
                jobs = [file_stamp(f)["mtime"] for f in sorted((self.proj / "jobs").glob("*.result.json"))]
            return {"src": self.src, "mode": mode, "series": self._series_terms_digest(), "jobs": jobs,
                    "model": self.cfg.endpoint()["model"] if mode == "api" else None}
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
            emit({"type": "stage", "name": name, "status": "cached", "seconds": rec.get("seconds")})
            return self.results[name]
        if self.cancel is not None and self.cancel.is_set():
            emit({"type": "stage", "name": name, "status": "cancelled"})
            raise Cancelled()
        emit({"type": "stage", "name": name, "status": "running"})
        t = time.time()
        try:
            result = fn()
        except Waiting:
            emit({"type": "stage", "name": name, "status": "waiting"})
            raise
        except Exception as e:
            emit({"type": "stage", "name": name, "status": "failed", "error": str(e)})
            raise
        save_json(self._out_path(name), result)
        rec = {"in": fp_in, "out": digest(result), "seconds": round(time.time() - t, 1)}
        save_json(self._fp_path(name), rec)
        self.results[name] = result
        self.fps[name] = rec
        self.ran.append(name)
        emit({"type": "stage", "name": name, "status": "done", "seconds": rec["seconds"]})
        return result

    def plan(self):
        """不运行，算出每一步的状态：done 可复用 / stale 要重做 / missing 还没跑过。界面改配置前用来预演。"""
        asr = load_json(self._out_path("asr"), {"language": None})
        subs = load_json(self._out_path("subs"), [])
        self.src = self._resolve_source(asr, subs, quiet=True)
        status = {}
        for name in DEFS:
            rec = load_json(self._fp_path(name))
            if not rec or not self._out_path(name).exists():
                status[name] = "missing"
                continue
            if any(status.get(d) != "done" for d in DEFS[name].deps):
                status[name] = "stale"
                continue
            for d in DEFS[name].deps:
                self.fps[d] = load_json(self._fp_path(d))
            status[name] = "done" if rec.get("in") == self.input_fp(name) else "stale"
        return [{"stage": n, "status": status[n], "seconds": (load_json(self._fp_path(n)) or {}).get("seconds")}
                for n in DEFS]

    def load_state(self):
        """读取已有的各步结果（不运行），并推断源语言。"""
        self.results = {n: r for n in DEFS if (r := load_json(self._out_path(n))) is not None}
        for n in DEFS:
            rec = load_json(self._fp_path(n))
            if rec:
                self.fps[n] = rec
        self.src = self._resolve_source(self.results.get("asr") or {"language": None},
                                        self.results.get("subs") or [], quiet=True)
        return self.results

    def render_context(self):
        """输出需要的东西：(字幕, 注音词, 画面比例, 硬字幕框)。先调 load_state。"""
        res = self.results
        info = res.get("probe") or {}
        aspect = (info.get("video") or {}).get("aspect") or 16 / 9
        fused = res.get("fuse")
        boxes = self._hardsub_boxes(res.get("ocr_refine") or [], fused) if fused else []
        ann = res.get("annotate") or {"cues": (res.get("translate") or {}).get("cues", []), "tokens": {}}
        return ann["cues"], ann["tokens"], aspect, boxes

    def reannotate(self):
        """只重做注音（编辑字幕、改读音词典后调用）：直接用已有的翻译结果。
        就算上游配置改过、翻译已经“过期”，也不会在这里重新识别或重新翻译（那要用户自己点运行）。"""
        self.load_state()
        translated = self.results.get("translate")
        if translated is None:
            raise RuntimeError("还没有翻译结果，先运行一次")
        self.results["annotate"] = self.stage("annotate", lambda: self._annotate(translated))
        return self

    def export(self, only=None):
        """只做输出（用已有结果）：only 是方案名列表，None = 全部启用的方案。返回输出文件名。"""
        self.load_state()
        cues, tokens, aspect, boxes = self.render_context()
        return self._render(cues, tokens, aspect, boxes, only)

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
            self.waiting = True
            return self._done(t_all)
        if self._stop("translate"):
            return self._done(t_all)

        annotated = self.stage("annotate", lambda: self._annotate(translated))
        if self._stop("annotate"):
            return self._done(t_all)
        self._render(annotated["cues"], annotated["tokens"], aspect, boxes)
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

    def _resolve_source(self, asr, subs, quiet=False):
        src = self.cfg.project.source
        if src != "auto":
            return lang.canonical(src)
        if asr.get("language"):
            return lang.canonical(asr["language"])
        guess = lang.detect("".join(c["text"] for c in subs[:200]))
        if guess:
            return guess
        if not quiet:
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

    def glossary_path(self):
        """术语表存哪：归入作品时存作品目录（多集共享），否则存项目目录。"""
        name = self.cfg.project.series
        return config_mod.series_dir(name) / "glossary.json" if name else self.proj / "glossary.json"

    def _series_terms(self):
        """翻译时已知的术语。不含这一集自己存进去的（否则存完术语，指纹一变就会重新翻译一遍）。"""
        terms = load_json(self.glossary_path(), {"terms": []})["terms"]
        return [t for t in terms if t.get("from") != self.proj.stem]

    def _series_terms_digest(self):
        return digest(self._series_terms())

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
        """把这一集新发现的术语存回术语表（记上来自哪一集）。"""
        path = self.glossary_path()
        saved = load_json(path, {"terms": []})["terms"]
        keep = [t for t in saved if t.get("from") != self.proj.stem]
        known = {t.get("src") for t in keep}
        new = [dict(t, **{"from": self.proj.stem}) for t in glossary.get("terms", []) if t.get("src") not in known]
        if not new and len(keep) == len(saved):
            return
        save_json(path, {"terms": keep + new})
        where = f"作品「{self.cfg.project.series}」" if self.cfg.project.series else "项目"
        log(f"{where}术语表已更新：本集新增 {len(new)} 条，共 {len(keep) + len(new)} 条")

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

    # ---------- 注音 ----------
    def _user_readings(self):
        """读音词典：用户全局 + 作品。{"entries": [{"lang": "ja", "src": "積丹", "reading": "しゃこたん", "proper": true}]}"""
        from .. import env

        files = [env.home() / "readings.json"]
        if self.cfg.project.series:
            files.insert(0, config_mod.series_dir(self.cfg.project.series) / "readings.json")  # 作品优先
        out = []
        for f in files:
            for e in load_json(f, {}).get("entries", []):
                out.append(dict(e, **{"from": "dict"}))
        return out

    def _readings(self, glossary):
        """优先级：读音词典 > 术语表读音（源语言）；同一写法只保留优先级高的。"""
        out, seen = [], set()
        terms = [{"lang": self.src, "src": t.get("src", ""), "reading": t.get("reading", ""),
                  "proper": t.get("type") in ("地名", "人名", "团体"), "from": "glossary"}
                 for t in (glossary or {}).get("terms", []) if t.get("reading") and t.get("reading") != "—"]
        for e in self._user_readings() + terms:
            k = (lang.canonical(e.get("lang") or self.src), e.get("src"))
            if e.get("src") and e.get("reading") and k not in seen:
                seen.add(k)
                out.append(e)
        return out

    def _annotate(self, translated):
        edits = load_json(self.proj / "edits.json", {})
        cues = apply_edits(translated["cues"], edits, self.src)
        edits = normalize_edits(edits)["cues"]
        readings = self._readings(translated.get("glossary"))
        warned = set()

        def ann(code, text):
            pack = lang.get(code)
            if not pack.annotations and not pack.translit_name:
                return None
            mine = [r for r in readings if lang.canonical(r.get("lang") or self.src) == pack.code]
            try:
                return pack.annotate(text.replace("｜", ""), mine)
            except ImportError as e:
                if code not in warned:
                    warned.add(code)
                    log(f"提示：{pack.name}注音需要额外依赖（{e.name}），请安装 nyasub[lang]；本次不注音")
                return None

        tokens, n_guess = {}, 0
        for c in cues:
            t = {"orig": ann(self.src, c["orig"]["text"]), "tr": {}}
            for code, v in c.get("tr", {}).items():
                w = ann(code, v["text"])
                if w is not None:
                    t["tr"][code] = w
            tokens[c["id"]] = t
            if t["orig"] and any(w["guess"] for w in t["orig"]):
                c["flags"] = list(c.get("flags", [])) + ["reading_guess"]
                if "review" not in edits.get(c["id"], {}):  # 用户手动标过的审校状态优先
                    c["review"] = "todo"
                n_guess += 1
        if any(tokens.values()):
            log(f"注音完成：{len(cues)} 条；读音词典 / 术语表 {len(readings)} 条；{n_guess} 条有读音待确认的专有名词")
        return {"cues": cues, "tokens": tokens}

    def _render(self, cues, tokens, aspect, boxes, only=None):
        from ..render import ass, layout, srt

        targets = {lang.canonical(t) for t in self.cfg.project.targets}
        seen = set()
        written = []
        for prof in self.cfg.render.profiles:
            if (only is None and not prof.enabled) or (only is not None and prof.name not in only):
                continue
            missing = {lang.canonical(ly.lang) for ly in prof.layers
                       if ly.role == "translation" and ly.lang and ly.show != "off"} - targets
            if missing:
                log(f"提示：方案「{prof.name}」用到了 {', '.join(sorted(missing))}，但 [project] targets 里没有，这些图层会是空的")
            name = self._profile_names(prof)
            if name in seen:
                name = name.rsplit(".", 1)[0] + f".{prof.name}.{prof.format}"
            seen.add(name)
            items = layout.build(cues, prof, self.cfg, self.src, tokens)
            path = self.proj.video.with_name(name)
            if prof.format == "ass":
                ass.write(str(path), items, prof, self.cfg.render.styles, aspect, boxes)
            else:
                srt.write(str(path), items)
            log(f"已输出「{prof.name}」：{name}")
            written.append(name)
        return written


def apply_edits(cues, edits: dict, src_lang=None):
    """手动修改（字段、拆分 / 合并 / 插入），见 core/edits.py。"""
    from .edits import apply

    return apply(cues, edits, src_lang)


def process(video, cfg, opts: Options, config_files=()):
    return Run(video, cfg, opts, config_files).run()
