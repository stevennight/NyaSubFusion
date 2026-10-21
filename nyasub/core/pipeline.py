"""流水线：probe → audio → subs → asr → ocr → ocr_refine → align → fuse → translate → annotate → render。

每一步的结果存在 `<stem>.nyasub/stages/<步骤>.json`，旁边的 `.fp` 记录指纹：
    输入指纹 = hash(算法版本 + 相关配置 + 额外输入（文件大小/时间等）+ 上游各步的输出指纹)
    输出指纹 = hash(这一步的结果内容)
输入指纹没变就直接复用；上游重跑但结果内容没变时，下游也不用重跑。
"""
import os
import re
import time
from dataclasses import dataclass, field

from .. import env, lang
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


Cancelled = env.Cancelled  # 用户取消：语音识别、画面字识别、大模型请求、输出视频都会在循环里及时停下


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
    "ocr_refine": StageDef(["ocr"], ["ocr.refine", "llm.vision_model", "llm.model"], version=2),
    # v2：截图里字没出全（渐入动画）时换一帧重新截图复核，不再把画面字截成半句
    "align": StageDef(["subs", "asr"], ["fusion.auto_sync"], version=2),  # v2：语音段先修时间（asr.repair）
    "fuse": StageDef(["align", "ocr_refine", "asr"], ["fusion"], version=2),  # v2：标记“同时段字幕在说别的话”的画面字
    "translate": StageDef(["fuse"], ["project.targets", "project.context", "llm.model", "llm.temperature",
                                     "llm.chunk_seconds", "llm.chunk_max_items", "render.lang_options"], version=4),
    # v4：原文照抄字幕的字；人名全部进术语表、各段沿用新译名、最后统一一遍
    # v3：“｜”只用来分屏、➡ 合句；没用进台词的画面字单独成条并补翻；名称用目标语言的常用名 / 学名
    # v2：没人说出来的画面字（标题、菜名）不和台词合并；说明类的位置框取所有来源的并集
    # 套用手动修改，再给原文和各译文注音（振假名、拼音…）
    "annotate": StageDef(["translate"], ["render.strip_marks"], version=4),  # v4：原文按段判断是否已在画面上；v3：说明类位置、去掉“ / ”；v2：去掉 ➡、分段时间
    # 输出视频（可选）：用排版好的字幕文件封装 MKV / 压制硬字幕
    "mux": StageDef(["annotate"], ["render.video.keep_subs", "render.video.fonts"]),
    "burn": StageDef(["annotate"], ["render.video.encoder", "render.video.quality", "render.video.height"]),
}
VIDEO_STAGES = ("mux", "burn")


def collect_inputs(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            from ..render.video import is_output

            out += sorted(os.path.join(p, n) for n in os.listdir(p)
                          if n.lower().endswith(VIDEO_EXTS + AUDIO_EXTS) and not is_output(n))  # 跳过自己输出的视频
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
        self.cancel = None      # threading.Event：设置后尽快停下（长步骤在循环里检查，见 env.check_cancel）
        self.written = []       # 本次输出的字幕：[(方案, 文件名)]
        self._video_profiles = None  # 这次输出视频指定的方案（None = 按配置）
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
        if name == "asr":  # 人声分离 / 云端识别开着时才有（都关着时和以前的指纹一样，不会重新识别）
            d = {"vocals": self._separation_model(), "fill_gaps": 1, "v": 2} if self._separate() else {}  # v2：按原音频判断语言
            if self.cfg.asr.backend == "api":
                ep = self.cfg.endpoint("asr")
                from ..sources.asr_api import chunk_seconds

                d["api"] = {"kind": self.cfg.asr.api_kind, "base_url": ep["base_url"], "model": ep["model"],
                            "chunk": chunk_seconds(self.cfg.asr.model_dump(), ep)}
            return d or None
        if name in VIDEO_STAGES:  # 视频文件 + 实际封装 / 压制进去的字幕内容
            return {"video": file_stamp(self.proj.video), "tracks": [
                {"lang": t["lang"], "title": t["title"],
                 "sub": digest(t["path"].read_text(encoding="utf-8-sig")) if t["path"].exists() else None}
                for t in self._video_tracks(name, self._video_profiles)]}
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
        outer = env.current_stage()
        env.set_stage(name)
        try:
            result = fn()
        except Waiting:
            emit({"type": "stage", "name": name, "status": "waiting"})
            raise
        except Cancelled:
            emit({"type": "stage", "name": name, "status": "cancelled"})
            raise
        except Exception as e:
            emit({"type": "stage", "name": name, "status": "failed", "error": str(e)})
            raise
        finally:
            self._save_stage_log(name, t)
            env.set_stage(outer)
        save_json(self._out_path(name), result)
        rec = {"in": fp_in, "out": digest(result), "seconds": round(time.time() - t, 1)}
        save_json(self._fp_path(name), rec)
        self.results[name] = result
        self.fps[name] = rec
        self.ran.append(name)
        emit({"type": "stage", "name": name, "status": "done", "seconds": rec["seconds"]})
        return result

    def _save_stage_log(self, name, t0):
        """这一步的日志存成 stages/<步骤>.log（界面上点步骤能看；程序重启后也在）。"""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))
        lines = [f"# {stamp}  用时 {time.time() - t0:.0f}s"] + env.stage_lines()
        try:
            self.proj.stage(name + ".log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    def stage_log(self, name):
        p = self.proj.stage(name + ".log")
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def plan(self):
        """不运行，算出每一步的状态：done 可复用 / stale 要重做 / missing 还没跑过。界面改配置前用来预演。"""
        asr = load_json(self._out_path("asr"), {"language": None})
        subs = load_json(self._out_path("subs"), [])
        self.src = self._resolve_source(asr, subs, quiet=True)
        status = {}
        for name in self._stage_names():
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
        out = []
        for n in self._stage_names():
            item = {"stage": n, "status": status[n], "seconds": (load_json(self._fp_path(n)) or {}).get("seconds")}
            off = self._off_reason(n)
            if off:  # 配置里关着：运行时这一步什么也不做（下游的“要不要重做”仍按真实状态算）
                item.update(status="off", note=off)
            elif status[n] == "done" and (empty := self._empty_note(n)):
                item.update(status="empty", note=empty)
            out.append(item)
        return out

    def _off_reason(self, name):
        """这一步在当前配置下不会做任何事：返回说明，否则 None。"""
        c = self.cfg
        info = load_json(self._out_path("probe")) or {}
        if name == "audio" and info and not info.get("has_audio"):
            return "没有音轨"
        if name == "subs" and (not c.subs.enabled or (self.opts.subs_path or "").lower() == "none"):
            return "未启用"
        if name == "asr" and not c.asr.enabled:
            return "未启用"
        if name == "ocr" and (not c.ocr.enabled or (info and not info.get("video"))):
            return "未启用"
        if name == "ocr_refine" and (not c.ocr.enabled or c.ocr.refine != "vlm"):
            return "未启用"
        if name == "align" and not c.fusion.auto_sync:
            return "未启用"
        if name in ("translate", "annotate") and self._llm_mode() == "off":
            return "不翻译"
        return None

    def _empty_note(self, name):
        """做过了，但什么也没找到（自带字幕、画面字、语音）。"""
        r = load_json(self._out_path(name))
        if name == "subs" and r == []:
            return "没有找到字幕"
        if name == "ocr" and r == []:
            return "没有画面字"
        if name == "asr" and isinstance(r, dict) and not r.get("segments"):
            return "没有语音"
        if name == "align" and isinstance(r, dict) and not r.get("subs"):
            return "没有字幕，不用对时"
        return None

    def render_status(self, plan=None):
        """输出字幕文件这一步：done 文件都在、而且比注音结果新 / stale 要重新输出 / missing 还没输出 / off 不翻译时不输出。"""
        plan = plan or self.plan()
        ann = next((p for p in plan if p["stage"] == "annotate"), None)
        if ann and ann["status"] == "off":
            return {"status": "off", "note": "不翻译", "files": []}
        names = self.output_names()
        files = [self.proj.video.with_name(n) for n in names]
        exist = [f for f in files if f.exists()]
        if not exist:
            return {"status": "missing", "files": []}
        ann_json = self._out_path("annotate")
        fresh = ann_json.exists() and min(f.stat().st_mtime for f in exist) >= ann_json.stat().st_mtime - 1
        done = ann and ann["status"] == "done" and len(exist) == len(files) and fresh
        return {"status": "done" if done else "stale", "files": [f.name for f in exist]}

    def _stage_names(self):
        """要显示的步骤：输出视频只算配置里开着的那一种。"""
        mode = self.cfg.render.video.mode
        return [n for n in DEFS if n not in VIDEO_STAGES or n == mode]

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

    def export_video(self, mode, profiles=None):
        """用已有结果输出字幕，再封装 MKV / 压制硬字幕。profiles：用哪些方案（None = 按配置）。"""
        self.load_state()
        if "annotate" not in self.results:
            raise RuntimeError("还没有字幕结果，先运行一次")
        cues, tokens, aspect, boxes = self.render_context()
        self._render(cues, tokens, aspect, boxes, profiles)
        env.set_cancel(self.cancel)
        try:
            self._video(mode, profiles)
        finally:
            env.set_cancel(None)
        return self

    def _round_stages(self):
        """这一轮运行会经过的步骤（按“运行到”截止；输出视频只在运行到最后时才做）。"""
        until = STAGES.index(self.opts.until)
        out = []
        for n in self._stage_names() + ["render"]:
            if n in VIDEO_STAGES:
                ok = self.opts.until == "render"
            else:  # 画面字复核和画面字识别一起做完才停
                ok = STAGES.index(n) <= until or (n == "ocr_refine" and self.opts.until == "ocr")
            if ok:
                out.append(n)
        return out

    def _stop(self, name):
        return STAGES.index(name) >= STAGES.index(self.opts.until)

    # ---------- 主流程 ----------
    def run(self):
        env.set_cancel(self.cancel)
        try:
            return self._run()
        finally:
            env.set_cancel(None)

    def _run(self):
        cfg, p = self.cfg, self.proj
        emit({"type": "round", "stages": self._round_stages()})  # 界面：这一轮会走到的步骤先显示成“待执行”
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

        asr = self._asr_fixed(self.stage("asr", self._run_asr))
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
            self._remember_outputs([os.path.basename(path)])
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
        emit({"type": "stage", "name": "render", "status": "running"})
        t = time.time()
        try:
            self._render(annotated["cues"], annotated["tokens"], aspect, boxes)
        except Exception as e:
            emit({"type": "stage", "name": "render", "status": "failed", "error": str(e)})
            raise
        emit({"type": "stage", "name": "render", "status": "done", "seconds": round(time.time() - t, 1)})
        if cfg.render.video.mode != "off":
            self._video(cfg.render.video.mode)
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
        return self.external_subs()

    def written_names(self):
        """本工具在这个视频旁边写出过的文件（每次输出都记下来；改了配置以后，旧的输出也认得出来）。"""
        return load_json(self.proj / "outputs.json", {"files": []})["files"]

    def _remember_outputs(self, names):
        known = self.written_names()
        merged = known + [n for n in names if n not in known]
        if merged != known:
            save_json(self.proj / "outputs.json", {"files": merged})

    def external_subs(self):
        """同名的外挂字幕（跳过本工具自己写出的）。"""
        from ..sources import subtitle

        legacy = not (self.proj / "outputs.json").exists()  # 旧项目没有输出记录：SRT 靠旁边同名的 ASS 认
        return subtitle.find_external(str(self.proj.video), skip_names=self.output_names() + self.written_names(),
                                      legacy_srt=legacy)

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

    def _asr_fixed(self, asr):
        """语音段修好时间再用（音乐和台词连在一起时第一个字会停在音乐开头，见 sources/asr.repair）。"""
        if not asr or not asr.get("segments"):
            return asr
        from ..sources.asr import repair

        return dict(asr, segments=repair(asr["segments"]))

    def _separate(self):
        """这次语音识别要不要先做人声分离：on / off 照办；auto = 没有字幕时（这时语音是主要来源）。"""
        c = self.cfg.asr
        if not c.enabled or c.separate == "off":
            return False
        if c.separate == "on":
            return True
        subs = self.results["subs"] if "subs" in self.results else load_json(self._out_path("subs"), None)
        return subs is not None and len(subs) == 0

    def _separation_model(self):
        from ..sources.vocals import MODEL

        return MODEL["name"]

    def _vocals(self, info):
        """分离出的人声（media/vocals.wav），同一个视频只做一次。"""
        from .. import gpu
        from ..sources import vocals

        out = self.proj.media("vocals.wav")
        stamp = {"video": file_stamp(self.proj.video), "model": vocals.MODEL["name"]}
        if out.exists() and load_json(self.proj.media("vocals.json")) == stamp:
            log("人声分离：复用已分离的人声")
            return out
        on_gpu, sec = vocals.separate(self.proj.video, out, info.get("duration") or 0.0, gpu=gpu.ocr_ready())
        save_json(self.proj.media("vocals.json"), stamp)
        log(f"人声分离完成，用时 {sec:.0f}s")
        return out

    def _run_asr(self):
        c = self.cfg.asr
        wav = (self.results.get("audio") or {}).get("wav")
        if not c.enabled or not wav:
            return {"segments": [], "language": None}
        from ..sources import asr

        separated = self._separate()
        if separated:
            wav = str(self._vocals(self.results["probe"]).relative_to(self.proj.root))
            if c.backend == "local" and c.vad_threshold > 0.15:  # 背景音乐去掉了，门槛放低也不会把音乐当成说话，能多找回小声的台词
                log(f"人声已分离：人声门槛 {c.vad_threshold} → 0.15")
                c = c.model_copy(update={"vad_threshold": 0.15})

        src = self.cfg.project.source
        whisper_lang = None if src == "auto" else lang.get(src).whisper
        info = self.results["probe"]
        junk = lang.get(src).hallucinations if src != "auto" else ()
        t = time.time()
        if c.backend == "api":  # 云端接口：没有本地模型，补识别（_fill_gaps）不做
            from ..sources import asr_api

            segs, detected = asr_api.transcribe(str(self.proj / wav), c.model_dump(), self.cfg.endpoint("asr"),
                                                whisper_lang, offset=info.get("audio_offset", 0.0),
                                                duration=info["duration"], junk=junk, cache_dir=self.proj / "llm_cache")
        else:
            segs, detected = asr.transcribe(str(self.proj / wav), c.model_dump(), whisper_lang,
                                            offset=info.get("audio_offset", 0.0), duration=info["duration"],
                                            fill_gaps=separated, junk=junk,
                                            detect_wav=self.proj / self.results["audio"]["wav"] if separated else None)
        # 只有别的（非拉丁）文字、完全没有这种语言文字的段丢掉：日语节目里冒出的“시청해주셔서 감사합니다”
        # （韩语的“谢谢观看”）是幻觉；英文歌词之类的拉丁字母不算
        pack = lang.get(lang.canonical(detected) if detected else src)
        foreign = re.compile(r"[가-힣぀-ヿ一-鿿Ѐ-ӿ؀-ۿ฀-๿]")
        kept = [x for x in segs if pack.meaningful.search(x["text"]) or not foreign.search(x["text"])]
        if len(kept) < len(segs):
            log(f"去掉 {len(segs) - len(kept)} 段不是{pack.name}的语音识别结果（多半是幻觉）")
        log(f"语音识别完成：{len(kept)} 段，用时 {time.time() - t:.0f}s")
        return {"segments": kept, "language": detected}

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
        return refine_ocr(events, self.cfg, str(self.proj.root), self.proj / "llm_cache", video=self.proj.video)

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
        cues = self._tidy(cues, edits)
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

    def _tidy(self, cues, edits):
        """去掉不要的符号（➡ 等）；整理“｜”分段：
        - 原文和译文段数对不上（大模型拿它当换行用）、或者本来就是一屏（只有一条来源）的，去掉“｜”，排版时按长度自己断；
          手动改过文字的条目不动。
        - 其余的每段按来源字幕算时间（parts）。"""
        from . import segments

        marks = [m for m in self.cfg.render.strip_marks if m]
        fused = self.results.get("fuse") or load_json(self._out_path("fuse"), {}) or {}
        items = {i["id"]: i for i in fused.get("items", [])}

        def clean(t):
            for m in marks:
                t = t.replace(m, "")
            # “ / ”是给大模型看的换行标记（提示词里把来源的换行写成“ / ”），大模型有时照抄进结果，
            # 或者自己用全角“／”把一条字幕的两行（两个人说的话、标签的两行）接起来：都换回换行。数字之间的“／”（1／2）不动
            t = re.sub(r" / |\s*(?<!\d)／(?!\d)\s*", "\n", t)
            return "\n".join(l.rstrip() for l in t.split("\n")).strip()

        from ..llm.translate import covered, fix_type, info_geometry, merge_dup_info

        cues = merge_dup_info([dict(c) for c in cues], keep=set(edits))  # 同一块画面字识别成两条的合成一条
        out = []
        for c in cues:
            orig = c["orig"]["text"]
            trs = {k: v.get("text", "") for k, v in (c.get("tr") or {}).items()}
            if not segments.bars_ok(c):  # 原文用“ / ”分段、译文用“｜”分段，段数一样：“ / ”就是分段
                orig = segments.reconcile(orig, trs) or orig
            c = dict(c, orig=dict(c["orig"], text=clean(orig)),
                     tr={k: dict(v, text=clean(v.get("text", ""))) for k, v in (c.get("tr") or {}).items()})
            c.pop("parts", None)
            c.pop("parts_onscreen", None)
            mine = edits.get(c["id"], {})
            if "type" not in mine:  # 没有画面字来源的“说明”其实是旁白（手动改过类型的不动）
                c["type"] = fix_type(c.get("type"), [items[s] for s in c.get("sources", []) if s in items])
            if c.get("type") in ("info", "name", "title") and mine.get("rect") is None:  # 手动选过位置的不动（None = 改回自动）
                # 说明类的位置按主要那条画面字算（旧结果里可能并上了画面另一处误认的字）；手动调过时间的不动时间
                srcs = [items[s] for s in c.get("sources", []) if s in items]
                geo = info_geometry(srcs)
                if geo:
                    c["rect"] = geo["rect"]
                    if srcs and all(s["src"] in ("telop", "info") for s in srcs) and "start" not in mine and "end" not in mine:
                        c["start"], c["end"] = geo["start"], geo["end"]
            touched = "orig" in edits.get(c["id"], {}) or "tr" in edits.get(c["id"], {})
            if "｜" in c["orig"]["text"] + "".join(v["text"] for v in c["tr"].values()) and not touched and (
                    not segments.bars_ok(c) or segments.single_event(c, items)):
                # 一屏放得下的（大模型拿“｜”当换行用）：都不分；放不下的长句：保留译文的“｜”当换屏的位置，
                # 原文按比例跟着切（比按行数硬切、断在半句中间好）
                long = not segments.single_event(c, items) and any(
                    len(segments.unbar(v["text"], k)) > 2 * segments.line_chars(k, self.cfg) for k, v in c["tr"].items())
                c["orig"]["text"] = segments.unbar(c["orig"]["text"], c["orig"]["lang"])
                if not long:
                    for k, v in c["tr"].items():
                        v["text"] = segments.unbar(v["text"], k)
            n_pieces = len(segments.pieces(c["orig"]["text"]))
            for key in ("parts_type", "parts_show"):  # 每段单独的设置：段数变了就不再适用
                if c.get(key) is not None and (len(c[key]) != n_pieces or n_pieces < 2):
                    c.pop(key)
            if segments.bars_ok(c):
                mine = edits.get(c["id"], {}).get("parts")  # 手动调过每段的时间
                c["parts"] = ((mine if segments.valid_parts(mine, c) else None) or segments.part_times(c, items)
                              or segments.proportional(c))
            # 画面上已经有原文的（硬字幕）：按段判断，只有那一段的字确实是画面字才不再显示原文；
            # 半句画面字、半句字幕的，字幕那部分的原文照常显示
            screen = [items[s]["text"] for s in c.get("sources", []) if s in items and items[s]["src"] in ("telop", "info")]
            if screen:
                ps = segments.pieces(c["orig"]["text"]) if c.get("parts") else [c["orig"]["text"]]
                shown = [covered(p, "\n".join(screen)) >= 0.8 for p in ps]
                c["onscreen"] = all(shown)
                if c.get("parts"):
                    c["parts_onscreen"] = shown
            out.append(c)
        return out

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
            self.written.append((prof, name))
        self._remember_outputs(written)
        return written

    # ---------- 输出视频 ----------
    def video_names(self):
        from ..render.video import BURN_SUFFIX, MUX_SUFFIX

        return {"mux": self.proj.stem + MUX_SUFFIX, "burn": self.proj.stem + BURN_SUFFIX}

    def _track(self, prof, name):
        targets = [lang.canonical(t) for t in self.cfg.project.targets] or [self.src]
        codes = []
        for ly in prof.layers:
            if ly.show == "off" or ly.role == "transliteration":
                continue
            c = lang.canonical(ly.lang or targets[0]) if ly.role == "translation" else self.src
            if c and c not in codes:
                codes.append(c)
        return {"path": self.proj.video.with_name(name), "lang": codes[0] if codes else self.src,
                "title": " / ".join(lang.get(c).name for c in codes) or prof.name, "profile": prof.name}

    def _video_tracks(self, mode, profiles=None):
        """要封装 / 压制的字幕：这次输出的（没输出过就按配置推算文件名）里，按指定方案挑。"""
        want = profiles or self.cfg.render.video.profiles
        written = self.written or [(p, self._profile_names(p)) for p in self.cfg.render.profiles if p.enabled]
        picked = [(p, n) for p, n in written if not want or p.name in want]
        if mode == "burn":
            picked = [(p, n) for p, n in picked if p.format == "ass"][:1]
        tracks = [self._track(p, n) for p, n in picked]
        dup = [sum(x["title"] == t["title"] for x in tracks) > 1 for t in tracks]
        for t, d in zip(tracks, dup):  # 名字重复时（比如同一种语言的 ASS 和 SRT）加上方案名
            if d:
                t["title"] += f"（{t['profile']}）"
        return tracks

    def _video(self, mode, profiles=None):
        from ..render import video

        vc = self.cfg.render.video
        self._video_profiles = profiles
        tracks = self._video_tracks(mode, profiles)
        if not tracks:
            raise RuntimeError("没有可用的字幕方案" + ("（压制硬字幕需要 ASS 方案）" if mode == "burn" else ""))
        out = self.proj.video.with_name(self.video_names()[mode])
        info = self.results.get("probe") or {}
        vinfo, duration = info.get("video"), info.get("duration") or 0.0

        def do():
            from ..sources.media import _need

            _need("ffmpeg")
            ass_files = [t["path"] for t in tracks if str(t["path"]).endswith(".ass")]
            fmode = "subset" if mode == "burn" else vc.fonts
            files, notes = video.collect_fonts(ass_files, self.proj.media(f"{mode}_fonts"), fmode)
            for n in notes:
                log(n)
            if files:
                kb = sum(f.stat().st_size for f in files) // 1024
                log(f"字体：{', '.join(f.name for f in files)}（{'子集，' if fmode == 'subset' else ''}共 {kb} KB）")
            t0 = time.time()
            try:
                if mode == "mux":
                    log(f"封装 MKV：{len(tracks)} 条字幕轨（{'、'.join(t['title'] for t in tracks)}）")
                    video.mux(self.proj.video, tracks, out, files, vc.keep_subs, vinfo, duration, self.cancel)
                    enc = "copy"
                else:
                    _, enc = video.burn(self.proj.video, tracks[0]["path"], files, out, vinfo, vc.encoder, vc.quality,
                                        vc.height, duration, self.cancel, self.proj.media("burn"))
            except video.Stopped:
                log("已取消输出视频")
                raise Cancelled()
            mb = out.stat().st_size / 1e6
            log(f"已输出视频：{out.name}（{mb:.0f} MB，用时 {time.time() - t0:.0f}s）")
            return {"file": out.name, "tracks": [{k: t[k] for k in ("lang", "title", "profile")} for t in tracks],
                    "fonts": [f.name for f in files], "encoder": enc}
        return self.stage(mode, do, force=not out.exists())


def apply_edits(cues, edits: dict, src_lang=None):
    """手动修改（字段、拆分 / 合并 / 插入），见 core/edits.py。"""
    from .edits import apply

    return apply(cues, edits, src_lang)


def process(video, cfg, opts: Options, config_files=()):
    return Run(video, cfg, opts, config_files).run()
