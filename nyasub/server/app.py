"""本地服务：界面通过 HTTP / WebSocket 调内核。

- 只监听 127.0.0.1；设置了 token 时，/api 和 /ws 都要带 token（桌面壳启动内核时传入）。
- 运行流水线放在后台线程里，事件（步骤开始/完成、日志）通过 WebSocket 推给界面。
- 编辑字幕、改读音、改样式这类操作只重做注音 / 输出，几百毫秒内返回。
"""
import asyncio
import copy
import hashlib
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from glob import escape as glob_escape
from pathlib import Path

import tomli_w
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__, env, lang
from ..core import config as config_mod
from ..core import pipeline
from ..core import rebase
from ..core.edits import Store as EditStore
from ..core.store import ProjectDir, load_json, save_json

def _web_dist() -> Path:
    """界面文件：NYASUB_WEB_DIST > 打包后的 web/ > 源码里的 web/dist。"""
    if os.environ.get("NYASUB_WEB_DIST"):
        return Path(os.environ["NYASUB_WEB_DIST"])
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "web"
    return Path(__file__).resolve().parents[2] / "web" / "dist"


WEB_DIST = _web_dist()


# ---------------- 项目登记 ----------------


def _registry_path():
    return env.home() / "projects.json"


def _pid(video):
    return hashlib.sha1(str(Path(video).resolve()).lower().encode("utf-8")).hexdigest()[:12]


class Projects:
    def __init__(self):
        self.lock = threading.Lock()

    def all(self):
        return load_json(_registry_path(), {"projects": []})["projects"]

    def get(self, pid):
        p = next((x for x in self.all() if x["id"] == pid), None)
        if not p:
            raise HTTPException(404, "没有这个项目")
        return p

    def add(self, video, workdir=None):
        video = str(Path(video).resolve())
        if not os.path.isfile(video):
            raise HTTPException(400, f"找不到文件：{video}")
        pid = _pid(video)
        with self.lock:
            items = [x for x in self.all() if x["id"] != pid]
            entry = {"id": pid, "video": video, "workdir": workdir, "opened": int(time.time())}
            items.insert(0, entry)
            save_json(_registry_path(), {"projects": items[:50]})
        return entry

    def remove(self, pid):
        """只从“最近项目”里去掉，不删任何文件。"""
        with self.lock:
            items = [x for x in self.all() if x["id"] != pid]
            save_json(_registry_path(), {"projects": items})


# ---------------- 后台任务与事件 ----------------


class Job:
    def __init__(self, pid):
        self.pid = pid
        self.status = "running"   # running / done / failed / waiting / cancelled
        self.error = None
        self.started = time.time()
        self.thread = None
        self.cancel = None        # threading.Event：取消（长步骤在循环里检查，几秒内停下）


class Hub:
    """把内核线程里发出的事件转给订阅了这个项目的 WebSocket。"""

    def __init__(self):
        self.loop = None
        self.subs: dict[str, set] = {}
        self.jobs: dict[str, Job] = {}
        self.backlog: dict[str, list] = {}
        self.rounds: dict[str, dict] = {}  # 每个项目这一轮的步骤（日志多了 backlog 会截掉开头，单独留一份）
        self.threads: dict[int, str] = {}  # 线程 → 项目
        env.on_event(self._on_event)

    def _on_event(self, ev):
        pid = self.threads.get(threading.get_ident())
        if pid:
            self.publish(pid, ev)

    def publish(self, pid, ev):
        ev = dict(ev, t=round(time.time(), 3))
        log = self.backlog.setdefault(pid, [])
        log.append(ev)
        if ev.get("type") == "round":
            self.rounds[pid] = ev
        del log[:-2000]
        if self.loop:
            for q in list(self.subs.get(pid, ())):
                self.loop.call_soon_threadsafe(q.put_nowait, ev)

    def start(self, pid, fn, cancel=None):
        job = self.jobs.get(pid)
        if job and job.status == "running":
            raise HTTPException(409, "这个项目正在运行")
        job = Job(pid)
        job.cancel = cancel
        self.jobs[pid] = job
        self.backlog[pid] = []
        self.rounds.pop(pid, None)

        def run():
            self.threads[threading.get_ident()] = pid
            try:
                result = fn()
                job.status = "waiting" if getattr(result, "waiting", False) else "done"
            except pipeline.Cancelled:
                job.status = "cancelled"
                env.log("已取消")
            except Exception as e:
                job.status, job.error = "failed", str(e)
                env.log(f"运行失败：{e}")
            finally:
                self.publish(pid, {"type": "job", "status": job.status, "error": job.error})
                self.threads.pop(threading.get_ident(), None)

        job.thread = threading.Thread(target=run, daemon=True, name=f"nyasub-{pid}")
        job.thread.start()
        return job

    def status(self, pid):
        job = self.jobs.get(pid)
        return {"status": job.status, "error": job.error, "started": job.started} if job else None


# ---------------- 应用 ----------------


def create_app(token: str | None = None) -> FastAPI:
    projects = Projects()
    hub = Hub()
    proxy_jobs: dict[str, dict] = {}  # 项目 → {"status": building / failed, "done", "total", "error"}

    @asynccontextmanager
    async def lifespan(_app):
        hub.loop = asyncio.get_running_loop()

        def quiet(loop, ctx):
            # 浏览器拖动视频时会中断分段请求，Windows 上会报 ConnectionResetError，属于正常现象
            if isinstance(ctx.get("exception"), ConnectionResetError):
                return
            loop.default_exception_handler(ctx)
        hub.loop.set_exception_handler(quiet)
        yield
        env.off_event(hub._on_event)

    app = FastAPI(title="NyaSubFusion", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        if token and request.url.path.startswith("/api") and \
                request.headers.get("x-nyasub-token") != token and request.query_params.get("token") != token:
            return JSONResponse({"detail": "token 不对"}, status_code=401)
        return await call_next(request)

    # ---------- 工具 ----------
    def project_paths(pid):
        p = projects.get(pid)
        proj = ProjectDir(p["video"], p.get("workdir"))
        return p, proj

    def load_cfg(proj):
        return config_mod.load(None, proj / "project.toml")

    def make_run(pid, until="render", redo=(), translate=None):
        p, proj = project_paths(pid)
        cfg, files = load_cfg(proj)
        opts = pipeline.Options(until=until, redo=set(redo), translate=translate, workdir=p.get("workdir"))
        return pipeline.Run(p["video"], cfg, opts, files)

    def summary(pid):
        p, proj = project_paths(pid)
        run = make_run(pid)
        run.load_state()
        from ..sources import media

        meta = run.results.get("probe")
        if not meta:  # 还没运行过：临时探测一次（ffprobe 很快），新项目也能马上框选识别区域、预览视频
            try:
                meta = media.probe(p["video"])
            except Exception:
                meta = None
        playable = media.browser_playable(p["video"], meta) if meta else None
        return {
            "id": pid, "video": p["video"], "name": Path(p["video"]).name, "workdir": str(proj.root),
            "meta": meta, "src": run.src, "plan": (plan := run.plan()), "render": run.render_status(plan), "job": hub.status(pid),
            "config_files": run.config_files, "outputs": run.output_names(),
            "videos": {m: n for m, n in run.video_names().items() if Path(p["video"]).with_name(n).exists()},
            "proxy": "direct" if playable else ("ready" if proj.media(media.PROXY_NAME).exists()
                                                or media.hls_state(proj.media(media.HLS_DIR))["complete"]
                                                else proxy_jobs.get(pid, {}).get("status", "none")),
        }

    # ---------- 基本 ----------
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.post("/api/shutdown")
    def shutdown():
        """桌面壳关闭前调用：优雅退出。"""
        server = getattr(app.state, "server", None)
        if hub.loop:  # 先让所有 WebSocket 正常断开，免得关闭时被强制取消
            for qs in hub.subs.values():
                for q in list(qs):
                    hub.loop.call_soon_threadsafe(q.put_nowait, None)
        if server:
            server.should_exit = True
        return {"ok": True, "running": [pid for pid, j in hub.jobs.items() if j.status == "running"]}

    # ---------- 全局设置（用户配置 + 凭据库） ----------
    def _user_cfg_path():
        return env.home() / "config.toml"

    @app.get("/api/settings")
    def get_settings():
        from .. import secrets

        path = _user_cfg_path()
        raw = config_mod.read_toml(path) if path.exists() else {}
        eff, _ = config_mod.load(None)
        return {"user_config": raw, "effective": eff.model_dump(mode="json"), "path": str(path),
                "keys": {p.name: bool(secrets.get(p.name)) for p in eff.llm.providers},
                "env_keys": {p.name: bool(p.api_key_env and os.environ.get(p.api_key_env)) for p in eff.llm.providers},
                "keyring": secrets.available(), "translate": {k: v for k, v in eff.endpoint().items() if k != "key"}}

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        raw = _clean(body.get("user_config") or {})
        for p in raw.get("llm", {}).get("providers", []):
            p.pop("api_key", None)  # Key 只进凭据库，不写进配置文件
        try:
            config_mod.Config.model_validate(raw)
        except Exception as e:
            raise HTTPException(400, f"设置不合法：{e}")
        path = _user_cfg_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomli_w.dumps(raw), encoding="utf-8")
        return get_settings()

    @app.put("/api/settings/keys/{name}")
    def put_key(name: str, body: dict = Body(...)):
        from .. import secrets

        key = (body.get("key") or "").strip()
        if not key:
            raise HTTPException(400, "Key 是空的")
        secrets.set(name, key)
        return {"ok": True}

    @app.delete("/api/settings/keys/{name}")
    def delete_key(name: str):
        from .. import secrets

        secrets.delete(name)
        return {"ok": True}

    @app.post("/api/settings/test")
    def test_provider(body: dict = Body(...)):
        """测试接口：{"name"} 用已保存的接口，或直接给 {"base_url", "model", "key"?, "name"?}。"""
        import time as _t

        from openai import OpenAI

        from .. import secrets

        eff, _ = config_mod.load(None)
        p = next((x for x in eff.llm.providers if x.name == body.get("name")), None)
        base_url = body.get("base_url") or (p.base_url if p else "")
        model = body.get("model") or (p.model if p else "")
        key = body.get("key") or (secrets.get(p.name) if p else "") or             (os.environ.get(p.api_key_env, "") if p and p.api_key_env else "")
        if not (base_url and model and key):
            return {"ok": False, "error": "缺少接口地址、模型或 API Key"}
        t0 = _t.time()
        try:
            client = OpenAI(base_url=base_url, api_key=key, timeout=30, max_retries=0)
            r = client.chat.completions.create(model=model, max_tokens=8,
                                               messages=[{"role": "user", "content": "回复“好”一个字"}])
            return {"ok": True, "ms": int((_t.time() - t0) * 1000), "reply": (r.choices[0].message.content or "")[:40]}
        except Exception as e:
            return {"ok": False, "ms": int((_t.time() - t0) * 1000), "error": str(e)[:300]}

    @app.get("/api/components")
    def get_components():
        from .. import components

        return dict(components.status(), job=hub.status("_components"))

    @app.post("/api/components/{name}")
    def install_component(name: str, body: dict = Body(default={})):
        """后台下载安装；进度通过 /ws/projects/_components 推送。"""
        from .. import components

        if name not in components.manifest():
            raise HTTPException(404, "没有这个组件")
        if body.get("remove"):
            return components.remove(name)
        hub.start("_components", lambda: components.install(name, body.get("index")))
        return {"started": True}

    @app.get("/api/asr_models")
    def asr_models():
        from ..sources.asr_models import catalog

        return catalog()

    @app.get("/api/langs")
    def langs():
        return [{"code": p.code, "name": p.name, "annotations": list(p.annotations), "translit": p.translit_name,
                 "line_chars": p.line_chars, "word_based": p.word_based} for p in lang.available()]

    @app.get("/api/schema/config")
    def schema():
        return config_mod.Config.model_json_schema()

    # ---------- 项目 ----------
    def _brief(p):
        """最近项目列表里一行要显示的：所在文件夹、时长、语言、做到哪一步、输出了几个字幕文件。只读小文件，很快。"""
        import tomllib

        video = Path(p["video"])
        proj_root = Path(p["workdir"]) if p.get("workdir") else video.with_name(video.stem + ".nyasub")
        info = load_json(proj_root / "stages" / "probe.json") or {}
        cfg = {}
        try:
            cfg = tomllib.loads((proj_root / "project.toml").read_text(encoding="utf-8")).get("project", {})
        except Exception:
            pass
        reached = next((n for n in reversed(pipeline.STAGES) if (proj_root / "stages" / f"{n}.json").exists()), None)
        outs = [f.name for f in video.parent.glob(glob_escape(video.stem) + ".*") if f.suffix.lower() in (".ass", ".srt")
                and "-merged." not in f.name] if video.parent.is_dir() else []
        job = hub.status(p["id"])
        return dict(p, name=video.name, folder=video.parent.name, exists=video.is_file(),
                    duration=info.get("duration"), source=cfg.get("source"), targets=cfg.get("targets"),
                    series=cfg.get("series") or "", reached=reached, outputs=outs,
                    running=bool(job and job["status"] == "running"), configured=(proj_root / "project.toml").exists())

    @app.get("/api/projects")
    def list_projects():
        return [_brief(p) for p in projects.all()]

    @app.delete("/api/projects/{pid}")
    def forget_project(pid: str):
        projects.remove(pid)
        return list_projects()

    @app.post("/api/projects/{pid}/reveal")
    def reveal_project(pid: str):
        """在资源管理器里打开视频所在的文件夹（选中这个视频）。"""
        import subprocess

        video = Path(projects.get(pid)["video"])
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(video)] if video.exists() else ["explorer", str(video.parent)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(video)])
        else:
            subprocess.Popen(["xdg-open", str(video.parent)])
        return {"ok": True}

    @app.post("/api/projects")
    def open_project(body: dict = Body(...)):
        """打开项目。还没配置过（没有 project.toml）时额外返回探测结果，界面据此显示新建向导。"""
        from ..sources import media

        entry = projects.add(body["path"], body.get("workdir"))
        out = summary(entry["id"])
        _, proj = project_paths(entry["id"])
        out["configured"] = (proj / "project.toml").exists()
        if not out["configured"]:
            try:
                info = media.probe(entry["video"])
                out["probe"] = {"duration": info["duration"], "video": info.get("video"), "has_audio": info.get("has_audio"),
                                "subtitles": info.get("subtitles"), "arib_ttml": bool(info.get("data_streams")),
                                "external_subs": make_run(entry["id"]).external_subs()}  # 跳过本工具以前的输出
            except Exception as e:
                out["probe"] = {"error": str(e)}
        return out

    @app.get("/api/presets")
    def get_presets():
        from .. import presets

        return presets.listing()

    @app.post("/api/projects/{pid}/setup")
    def setup_project(pid: str, body: dict = Body(...)):
        """新建向导：{"preset", "source", "targets", "series", "context"} → 写 project.toml。"""
        from .. import presets

        cfg = presets.build(body.get("preset") or "custom", body.get("source") or "auto", body.get("targets"),
                            body.get("series") or "", body.get("context") or "")
        return put_config(pid, {"project": cfg})

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        return summary(pid)

    @app.get("/api/projects/{pid}/plan")
    def get_plan(pid: str):
        run = make_run(pid)
        return run.plan()

    # ---------- 配置 ----------
    @app.get("/api/projects/{pid}/config")
    def get_config(pid: str):
        _, proj = project_paths(pid)
        cfg, files = load_cfg(proj)
        pf = proj / "project.toml"
        raw = config_mod.read_toml(pf) if pf.exists() else {}
        return {"project": raw, "effective": cfg.model_dump(mode="json"), "files": files}

    @app.put("/api/projects/{pid}/config")
    def put_config(pid: str, body: dict = Body(...)):
        _, proj = project_paths(pid)
        raw = body.get("project") or {}
        try:
            # 先校验：用户层 + 作品层 + 新的项目层合起来要合法
            pf_tmp = proj / "project.toml.check"
            pf_tmp.write_text(tomli_w.dumps(_clean(raw)), encoding="utf-8")
            config_mod.load(None, pf_tmp)
        except Exception as e:
            raise HTTPException(400, f"配置不合法：{e}")
        finally:
            if pf_tmp.exists():
                pf_tmp.unlink()
        (proj / "project.toml").write_text(tomli_w.dumps(_clean(raw)), encoding="utf-8")
        return {"config": get_config(pid), "plan": get_plan(pid)}

    # ---------- 运行 ----------
    @app.post("/api/projects/{pid}/run")
    def run_project(pid: str, body: dict = Body(default={})):
        run = make_run(pid, body.get("until") or "render", body.get("redo") or (), body.get("translate"))
        run.cancel = threading.Event()
        hub.start(pid, run.run, run.cancel)
        return {"started": True}

    @app.post("/api/projects/{pid}/cancel")
    def cancel_project(pid: str):
        job = hub.jobs.get(pid)
        if not job or job.status != "running":
            raise HTTPException(409, "没有在运行")
        if job.cancel is None:
            raise HTTPException(400, "这个任务不能取消")
        job.cancel.set()
        env.log("正在停止…")
        return {"ok": True}

    @app.get("/api/projects/{pid}/stages/{name}/log")
    def get_stage_log(pid: str, name: str):
        """某一步上次运行的日志（存在 stages/<步骤>.log）。"""
        if name not in pipeline.DEFS:
            raise HTTPException(404, "没有这一步")
        return {"lines": make_run(pid).stage_log(name)}

    @app.get("/api/projects/{pid}/log")
    def get_log(pid: str):
        """这一轮的事件（日志、步骤状态、进度）。开头被截掉时补上这一轮的步骤列表。"""
        evs = hub.backlog.get(pid, [])
        r = hub.rounds.get(pid)
        return ([r] if r and evs and r not in evs and r["t"] <= evs[0]["t"] else []) + evs

    @app.websocket("/ws/projects/{pid}")
    async def ws(socket: WebSocket, pid: str):
        if token and socket.query_params.get("token") != token:
            await socket.close(code=4401)
            return
        await socket.accept()
        q: asyncio.Queue = asyncio.Queue()
        hub.subs.setdefault(pid, set()).add(q)
        try:
            await socket.send_json({"type": "hello", "job": hub.status(pid)})
            while True:
                ev = await q.get()
                if ev is None:  # 服务要关闭了
                    await socket.close()
                    break
                await socket.send_json(ev)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.subs.get(pid, set()).discard(q)

    # ---------- 数据 ----------
    @app.get("/api/projects/{pid}/data")
    def get_data(pid: str):
        run = make_run(pid)
        res = run.load_state()
        cues, tokens, aspect, boxes = run.render_context()
        fused = res.get("fuse") or {}
        marks = fused.get("ocr_marks", {})
        ocr = [dict(e, **marks.get(e["id"], {})) for e in res.get("ocr_refine") or []]
        return {
            "meta": res.get("probe"), "src": run.src, "targets": [lang.canonical(t) for t in run.cfg.project.targets],
            "aspect": aspect, "hardsub": boxes,
            "subs": (res.get("align") or {}).get("subs", res.get("subs") or []),
            "ocr": ocr, "asr": (run._asr_fixed(res.get("asr")) or {}).get("segments", []),
            "fused": {"items": fused.get("items", []), "stats": fused.get("stats")},
            "cues": cues, "tokens": tokens, "glossary": (res.get("translate") or {}).get("glossary"),
            "edits": EditStore(run.proj).load(), "history": EditStore(run.proj).depth(),
            # 重新翻译后对不上、没有套用的手动修改（审校页列出来让用户重做）
            "orphans": [rebase.describe(o) for o in EditStore(run.proj).load().get("orphans", [])],
        }

    @app.get("/api/projects/{pid}/preview")
    def preview(pid: str, profile: str | None = None):
        """按输出方案排好的字幕（和导出的 ASS 同一套位置），界面直接叠在视频上。"""
        from ..render import ass, fonts, layout

        run = make_run(pid)
        run.load_state()
        cues, tokens, aspect, boxes = run.render_context()
        profs = run.cfg.render.profiles
        prof = next((p for p in profs if p.name == profile), None) or (profs[0] if profs else None)
        if not prof or not run.src:
            return {"profile": None, "items": []}
        items = layout.build(cues, prof, run.cfg, run.src, tokens)
        px, py, used, placed = ass.place(items, prof, run.cfg.render.styles, aspect, boxes)
        out = []
        for p in placed:
            it = p["item"]
            base = {"kind": p["kind"], "start": it["start"], "end": it["end"], "sources": it.get("sources", []),
                    "cue": it.get("cue"), "moved": bool(it.get("pos"))}
            if p["kind"] == "info":
                base.update(style=prof.info_style, lines=[layout.plain(l) for l in it["layers"][0][2]],
                            top=bool(p.get("top")), an=p.get("an"), x=p.get("x"), y=p.get("y"))
            else:
                base.update(margin_v=p["margin_v"], type=it.get("type"), x=p.get("x"), y=p.get("y"), rows=[
                    {"style": r["ly"].style, "role": r["ly"].role, "chunks": [[t, rt] for t, rt, _, _ in r["chunks"]],
                     "rsize": r["rsize"], "rcolor": r["ly"].annotate.color, "height": r["height"]} for r in p["rows"]])
            out.append(base)
        return {"profile": prof.name, "profiles": [x.name for x in profs], "px": px, "py": py,
                "styles": {k: {**v.model_dump(), "em_scale": fonts.metrics(v.font).em_scale} for k, v in used.items()},
                "items": out}

    def _reannotate(pid):
        run = make_run(pid)
        try:
            run.reannotate()
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        run.load_state()
        return run

    def _edit_result(pid, cid=None):
        run = _reannotate(pid)
        cues, tokens, _, _ = run.render_context()
        cue = next((c for c in cues if c["id"] == cid), None) if cid else None
        return {"cue": cue, "tokens": tokens.get(cid) if cid else None, "history": EditStore(run.proj).depth()}

    @app.patch("/api/projects/{pid}/cues/{cid}")
    def edit_cue(pid: str, cid: str, body: dict = Body(...)):
        """改字幕：{"orig": "...", "tr": {"zh-Hans": "..."}, "start", "end", "type", "review", "delete"}"""
        _, proj = project_paths(pid)
        EditStore(proj).set_fields(cid, body)
        return _edit_result(pid, cid)

    def _current_cues(pid):
        run = make_run(pid)
        run.load_state()
        return run, run.render_context()[0]

    @app.post("/api/projects/{pid}/cues/{cid}/split")
    def split_cue(pid: str, cid: str, body: dict = Body(...)):
        """在 {"at": 秒} 处拆成两条，文字按时间比例切开（之后可以再改）。"""
        from ..core.edits import split_op

        run, cues = _current_cues(pid)
        cue = next((c for c in cues if c["id"] == cid), None)
        if not cue:
            raise HTTPException(404, "没有这条字幕")
        at = float(body["at"])
        if not (cue["start"] + 0.2 < at < cue["end"] - 0.2):
            raise HTTPException(400, "拆分点要在这条字幕中间（离两端至少 0.2 秒）")
        EditStore(run.proj).add_op(split_op(cue, at))
        return _edit_result(pid, cid + "a")

    @app.post("/api/projects/{pid}/cues/{cid}/pieces")
    def cue_pieces(pid: str, cid: str, body: dict = Body(...)):
        """“｜”分段的操作：{"action": "split", "at": k 或 "all"} 在第 k 个分界处拆成单独的字幕；
        {"action": "join", "at": k} 把第 k 个分界前后两段合成一段；
        {"action": "cut", "t": 秒} 在 t 秒处把所在的那一段分成两段（还是同一条字幕）；
        {"action": "type", "seg": i, "type": 类型} / {"action": "show", "seg": i, "show": "on" | "off" | null}
        单独设第 i 段（从 0 起）的类型 / 原文显不显示；设成说明类的，那一段拆出来单独成一条（说明要单独定位置）。"""
        from ..core import segments
        from ..core.edits import piece_cut_fields, piece_isolate_ops, piece_join_fields, piece_split_ops

        run, cues = _current_cues(pid)
        cue = next((c for c in cues if c["id"] == cid), None)
        if not cue:
            raise HTTPException(404, "没有这条字幕")
        if body.get("action") in ("type", "show"):
            n = len(segments.pieces(cue["orig"]["text"]))
            seg = int(body.get("seg", -1))
            if n < 2 or not 0 <= seg < n:
                raise HTTPException(400, "这条字幕没有这一段")
            store = EditStore(run.proj)
            if body["action"] == "type" and body.get("type") in ("info", "name", "title"):
                ops, nid = piece_isolate_ops(cue, seg)
                e = store.load()
                e["ops"] += ops
                e["cues"][nid] = {**e["cues"].get(nid, {}), "type": body["type"]}
                store.save(e)  # 拆出来 + 改类型算一次修改
                return _edit_result(pid, nid)
            key, val = ("parts_type", body.get("type")) if body["action"] == "type" else ("parts_show", body.get("show"))
            lst = list(cue.get(key) or [None] * n)
            lst[seg] = val or None
            store.set_fields(cid, {key: lst if any(x is not None for x in lst) else None})
            return _edit_result(pid, cid)
        if body.get("action") == "cut":
            fields = piece_cut_fields(cue, float(body["t"]))
            if not fields:
                raise HTTPException(400, "播放头要在某一段中间（离两端至少 0.2 秒），而且原文、译文的段数要对得上")
            EditStore(run.proj).set_fields(cid, fields)
            return _edit_result(pid, cid)
        n = len(segments.pieces(cue["orig"]["text"]))
        at = body.get("at")
        ks = list(range(1, n)) if at == "all" else [int(at)]
        if n < 2 or any(not 0 < k < n for k in ks):
            raise HTTPException(400, "这条字幕没有这个分段")
        store = EditStore(run.proj)
        if body.get("action") == "join":
            store.set_fields(cid, piece_join_fields(cue, ks[0]))
            return _edit_result(pid, cid)
        ops, first = piece_split_ops(cue, ks)
        e = store.load()
        e["ops"] += ops
        store.save(e)  # 几步拆分算一次修改，撤销一次就回来
        return _edit_result(pid, first)

    @app.post("/api/projects/{pid}/cues/{cid}/merge")
    def merge_cue(pid: str, cid: str):
        """和下一条合并。"""
        run, cues = _current_cues(pid)
        i = next((k for k, c in enumerate(cues) if c["id"] == cid), None)
        if i is None or i + 1 >= len(cues):
            raise HTTPException(400, "后面没有字幕可以合并")
        EditStore(run.proj).add_op({"op": "merge", "ids": [cid, cues[i + 1]["id"]]})
        return _edit_result(pid, cid)

    @app.post("/api/projects/{pid}/cues")
    def insert_cue(pid: str, body: dict = Body(...)):
        """插入一条：{"start", "end", "orig"?, "tr"?, "type"?}"""
        from ..core.edits import next_insert_id

        run, _ = _current_cues(pid)
        store = EditStore(run.proj)
        nid = next_insert_id(store.load())
        store.add_op({"op": "insert", "id": nid, "start": float(body["start"]), "end": float(body["end"]),
                      "type": body.get("type", "dialogue"), "orig": body.get("orig", ""), "tr": body.get("tr") or {}})
        return _edit_result(pid, nid)

    @app.delete("/api/projects/{pid}/edits/orphans")
    def clear_orphans(pid: str):
        """用户看过了：清掉对不上的修改列表。"""
        _, proj = project_paths(pid)
        store = EditStore(proj)
        e = store.load()
        e.pop("orphans", None)
        store.write_quiet(e)
        return {"ok": True}

    @app.post("/api/projects/{pid}/edits/{action}")
    def undo_redo(pid: str, action: str):
        if action not in ("undo", "redo"):
            raise HTTPException(404)
        from ..core.edits import describe_change

        _, proj = project_paths(pid)
        store = EditStore(proj)
        before = store.load()
        if (store.undo() if action == "undo" else store.redo()) is None:
            raise HTTPException(400, "没有可以撤销的修改" if action == "undo" else "没有可以重做的修改")
        change = describe_change(before, store.load(), undone=action == "undo")
        res = _edit_result(pid, change["id"])
        if change["id"] and res["cue"] is None:  # 那一条已经不在了（被拆分 / 合并掉）：只跳时间
            change["id"] = None
        return {**res, "changed": change}

    # ---------- 术语与读音词典 ----------
    @app.get("/api/series")
    def list_series():
        root = env.home() / "series"
        out = []
        for d in sorted(root.iterdir()) if root.is_dir() else []:
            if d.is_dir():
                terms = load_json(d / "glossary.json", {"terms": []})["terms"]
                out.append({"name": d.name, "terms": len(terms), "episodes": len({t.get("from") for t in terms} - {None, "manual"})})
        return out

    @app.get("/api/projects/{pid}/glossary")
    def get_glossary(pid: str):
        run = make_run(pid)
        run.load_state()
        saved = load_json(run.glossary_path(), {"terms": []})["terms"]
        gl = (run.results.get("translate") or {}).get("glossary") or {}
        known = {t.get("src") for t in saved}  # 翻译结果里还没存进术语表的（旧项目），也列出来
        saved = saved + [dict(t, **{"from": run.proj.stem}) for t in gl.get("terms", []) if t.get("src") not in known]
        return {"series": run.cfg.project.series, "context": run.cfg.project.context, "summary": gl.get("summary", ""),
                "path": str(run.glossary_path()), "episode": run.proj.stem, "terms": saved}

    @app.put("/api/projects/{pid}/glossary")
    def put_glossary(pid: str, body: dict = Body(...)):
        """整表替换：{"terms": [{"src", "reading", "tr", "type", "note", "from"}]}。改了要重新翻译才生效。"""
        run = make_run(pid)
        terms = []
        for t in body.get("terms") or []:
            if (t.get("src") or "").strip():
                terms.append({k: v for k, v in t.items() if k in ("src", "reading", "tr", "type", "note", "from") and v not in (None, "")}
                             | {"from": t.get("from") or "manual"})
        save_json(run.glossary_path(), {"terms": terms})
        return {"terms": terms, "plan": run.plan()}

    def _reading_files(run):
        files = {"global": env.home() / "readings.json"}
        if run.cfg.project.series:
            files["series"] = config_mod.series_dir(run.cfg.project.series) / "readings.json"
        return files

    @app.get("/api/projects/{pid}/readings")
    def get_readings(pid: str):
        run = make_run(pid)
        return {k: load_json(f, {"entries": []})["entries"] for k, f in _reading_files(run).items()}

    @app.put("/api/projects/{pid}/readings")
    def put_readings(pid: str, body: dict = Body(...)):
        """{"global": [...], "series": [...]}：整表替换，然后重新注音。"""
        run = make_run(pid)
        for k, f in _reading_files(run).items():
            if k in body:
                entries = [{kk: vv for kk, vv in e.items() if kk in ("lang", "src", "reading", "proper")}
                           for e in body[k] if (e.get("src") or "").strip() and (e.get("reading") or "").strip()]
                save_json(f, {"entries": entries})
        _reannotate(pid)
        return get_readings(pid)

    @app.post("/api/projects/{pid}/readings")
    def add_reading(pid: str, body: dict = Body(...)):
        """{"lang": "ja", "src": "積丹", "reading": "しゃこたん", "scope": "series" | "global"}"""
        run = make_run(pid)
        series = run.cfg.project.series
        scope = body.get("scope") or ("series" if series else "global")
        path = (config_mod.series_dir(series) if scope == "series" and series else env.home()) / "readings.json"
        data = load_json(path, {"entries": []})
        entry = {k: body[k] for k in ("lang", "src", "reading") if body.get(k)}
        if "proper" in body:
            entry["proper"] = bool(body["proper"])
        data["entries"] = [e for e in data["entries"] if not (e.get("src") == entry["src"] and e.get("lang") == entry.get("lang"))]
        data["entries"].insert(0, entry)
        save_json(path, data)
        _reannotate(pid)
        return {"saved": str(path), "entries": len(data["entries"])}

    @app.post("/api/projects/{pid}/cues/{cid}/retranslate")
    def retranslate(pid: str, cid: str, body: dict = Body(default={})):
        from ..llm.translate import retranslate as do

        run = make_run(pid)
        run.load_state()
        if run.cfg.llm_mode() != "api":
            raise HTTPException(400, "没有配置大模型接口，不能单条重译")
        cues, _, _, _ = run.render_context()
        i = next((k for k, c in enumerate(cues) if c["id"] == cid), None)
        if i is None:
            raise HTTPException(404, "没有这条字幕")
        tgt = lang.canonical(body.get("lang") or run.cfg.project.targets[0])
        terms = ((run.results.get("translate") or {}).get("glossary") or {}).get("terms", [])
        items = {it["id"]: it for it in (run.results.get("fuse") or {}).get("items", [])}
        # 用户手动改过原文（这一条，或者拆分前的那一条）：重译以改后的原文为准，来源只看时间和语境
        fields = EditStore(run.proj).load()["cues"]
        base = rebase.split_id(cid)[0]
        edited = "orig" in fields.get(cid, {}) or (base is not None and "orig" in fields.get(base, {}))
        alts = do(run.cfg, run.proj / "llm_cache", cues[i], cues[max(0, i - 3): i + 4], run.src, tgt, terms,
                  body.get("instruction", ""), [items[x] for x in cues[i].get("sources", []) if x in items],
                  orig_edited=edited)
        return {"alts": alts, "lang": tgt}

    @app.post("/api/projects/{pid}/export")
    def export(pid: str, body: dict = Body(default={})):
        run = make_run(pid)
        names = run.export(body.get("profiles"))
        return {"files": names, "dir": str(Path(run.proj.video).parent)}

    @app.post("/api/projects/{pid}/export_video")
    def export_video(pid: str, body: dict = Body(...)):
        """{"mode": "mux" | "burn", "profiles": [方案名]（可选）}：后台输出视频，进度推到项目频道。"""
        mode = body.get("mode")
        if mode not in pipeline.VIDEO_STAGES:
            raise HTTPException(400, "mode 只能是 mux / burn")
        run = make_run(pid)
        run.cancel = threading.Event()
        hub.start(pid, lambda: run.export_video(mode, body.get("profiles") or None), run.cancel)
        return {"started": True, "file": run.video_names()[mode]}

    # ---------- 媒体 ----------
    def _media_info(pid):
        from ..sources import media

        p, proj = project_paths(pid)
        info = load_json(proj.stage("probe.json"))
        if info is None:
            info = media.probe(p["video"])
        return p, proj, info

    @app.get("/api/projects/{pid}/media/video")
    def video(pid: str):
        """能直接播放的原视频，或者旧版生成的 proxy2.mp4。其他情况走 /media/source + HLS。"""
        from ..sources import media

        p, proj, info = _media_info(pid)
        if media.browser_playable(p["video"], info):
            return FileResponse(p["video"])
        if proj.media(media.PROXY_NAME).exists():
            return FileResponse(proj.media(media.PROXY_NAME), media_type="video/mp4")
        raise HTTPException(404, "这个视频要先转预览：见 /media/source")

    @app.get("/api/projects/{pid}/media/source")
    def media_source(pid: str):
        """界面怎么播放：direct 原视频 / mp4 旧版预览文件 / hls 边转边看的分段（没转过就开始转）。"""
        from ..sources import media

        p, proj, info = _media_info(pid)
        if media.browser_playable(p["video"], info):
            return {"kind": "direct"}
        if proj.media(media.PROXY_NAME).exists():
            return {"kind": "mp4"}
        if not info.get("video"):
            return {"kind": "none", "status": "failed", "error": "没有视频画面"}
        out = proj.media(media.HLS_DIR)
        st = media.hls_state(out)
        total = float(info.get("duration") or 0)
        if st["complete"]:
            return {"kind": "hls", "status": "ready", **st, "done": total, "total": total}
        job = proxy_jobs.get(pid)
        if job and job["status"] == "failed":
            proxy_jobs.pop(pid, None)  # 报一次错；下次打开再试
            return {"kind": "hls", "status": "failed", "error": job["error"], **st}
        if not job:  # 没在转（第一次打开，或者上次没转完程序就关了）：从头转
            job = proxy_jobs[pid] = {"status": "building", "done": 0.0, "total": total, "error": None}

            def build():
                for old in ("proxy.mp4", "proxy.tmp.mp4", "proxy2.tmp.mp4"):  # 旧版留下的
                    proj.media(old).unlink(missing_ok=True)
                try:
                    def prog(done, total):
                        job["done"] = round(done, 1)
                    how = media.make_hls(p["video"], out, info.get("video"), total, prog, log=env.log)
                    env.log(f"预览视频已生成（{how}）")
                    proxy_jobs.pop(pid, None)
                except Exception as e:
                    job.update(status="failed", error=str(e)[-400:])
                    env.log(f"生成预览视频失败：{e}")
            threading.Thread(target=build, daemon=True, name=f"nyasub-proxy-{pid}").start()
            st = {"segments": 0, "seconds": 0.0, "complete": False}
        return {"kind": "hls", "status": "building", **st, "done": job["done"], "total": job["total"]}

    @app.get("/api/projects/{pid}/media/hls/{name}")
    def media_hls(pid: str, name: str):
        import re

        from ..sources import media

        if not re.fullmatch(r"index\.m3u8|seg_\d{5}\.ts", name):
            raise HTTPException(404)
        f = project_paths(pid)[1].media(media.HLS_DIR) / name
        if not f.exists():
            raise HTTPException(404)
        if name.endswith(".m3u8"):  # 转换中会一直变：不缓存
            return FileResponse(f, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
        return FileResponse(f, media_type="video/mp2t")

    @app.get("/api/projects/{pid}/media/frame")
    def frame(pid: str, t: float = 0.0, h: int = 360):
        """某一时刻的画面（JPEG），框选识别区域用。"""
        import subprocess

        from fastapi.responses import Response

        p, _ = project_paths(pid)
        cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", p["video"], "-frames:v", "1",
               "-vf", f"scale=-2:{max(90, min(h, 1080))}", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode != 0 or not r.stdout:
            raise HTTPException(500, f"取画面失败：{r.stderr.decode('utf-8', 'replace')[-200:]}")
        return Response(r.stdout, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/projects/{pid}/media/peaks")
    def peaks(pid: str):
        from ..sources import media

        _, proj = project_paths(pid)
        cache, wav = proj.media("peaks.json"), proj.media("audio.wav")
        if not wav.exists():
            return {"per_second": 50, "peaks": []}
        data = load_json(cache)
        if not data or data.get("size") != wav.stat().st_size:
            data = dict(media.waveform_peaks(wav), size=wav.stat().st_size)
            save_json(cache, data)
        return data

    @app.get("/api/projects/{pid}/crops/{name}")
    def crop(pid: str, name: str):
        _, proj = project_paths(pid)
        path = (proj / "crops" / name).resolve()
        if path.parent != (proj / "crops").resolve() or not path.exists():
            raise HTTPException(404)
        return FileResponse(path)

    # ---------- 批量队列 ----------
    from .queue import Queue

    def _setup_from_options(pid, opts):
        from .. import presets

        cfg = presets.build(opts.get("preset") or "documentary", opts.get("source") or "auto", opts.get("targets"),
                            opts.get("series") or "", opts.get("context") or "")
        put_config(pid, {"project": cfg})

    queue = Queue(hub, projects, make_run, _setup_from_options,
                  lambda pid: (project_paths(pid)[1] / "project.toml").exists())
    app.state.queue = queue

    @app.get("/api/queue")
    def get_queue():
        return queue.state()

    @app.post("/api/queue")
    def add_to_queue(body: dict = Body(...)):
        """{"paths": [文件或文件夹], "options": {"preset", "source", "targets", "series", "until", "translate"}}"""
        videos = pipeline.collect_inputs(body.get("paths") or [])
        if not videos:
            raise HTTPException(400, "没有找到视频 / 音频文件")
        added = queue.add(videos, body.get("options") or {})
        return {"added": len(added), "skipped": len(videos) - len(added), **queue.state()}

    @app.post("/api/queue/pause")
    def pause_queue(body: dict = Body(...)):
        queue.set_paused(body.get("paused", True))
        return queue.state()

    @app.post("/api/queue/clear")
    def clear_queue():
        queue.clear_finished()
        return queue.state()

    @app.post("/api/queue/{item_id}/{action}")
    def queue_item(item_id: str, action: str):
        try:
            if action == "remove":
                queue.remove(item_id)
            elif action in ("up", "down"):
                queue.move(item_id, -1 if action == "up" else 1)
            elif action == "retry":
                queue.retry(item_id)
            elif action == "cancel":
                queue.cancel(item_id)
            else:
                raise HTTPException(404)
        except KeyError:
            raise HTTPException(404, "队列里没有这一项")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return queue.state()

    # ---------- 界面 ----------
    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    app.state.hub = hub
    return app


def _clean(d):
    """tomli_w 不接受 None：去掉值为 None 的键。"""
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_clean(v) for v in d]
    return copy.deepcopy(d)
