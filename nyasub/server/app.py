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
from pathlib import Path

import tomli_w
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__, env, lang
from ..core import config as config_mod
from ..core import pipeline
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


# ---------------- 后台任务与事件 ----------------


class Job:
    def __init__(self, pid):
        self.pid = pid
        self.status = "running"   # running / done / failed / waiting
        self.error = None
        self.started = time.time()
        self.thread = None


class Hub:
    """把内核线程里发出的事件转给订阅了这个项目的 WebSocket。"""

    def __init__(self):
        self.loop = None
        self.subs: dict[str, set] = {}
        self.jobs: dict[str, Job] = {}
        self.backlog: dict[str, list] = {}
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
        del log[:-2000]
        if self.loop:
            for q in list(self.subs.get(pid, ())):
                self.loop.call_soon_threadsafe(q.put_nowait, ev)

    def start(self, pid, fn):
        job = self.jobs.get(pid)
        if job and job.status == "running":
            raise HTTPException(409, "这个项目正在运行")
        job = Job(pid)
        self.jobs[pid] = job
        self.backlog[pid] = []

        def run():
            self.threads[threading.get_ident()] = pid
            try:
                result = fn()
                job.status = "waiting" if getattr(result, "waiting", False) else "done"
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
    proxy_jobs: dict[str, str] = {}  # 项目 → building / failed

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
        meta = run.results.get("probe")
        playable = None
        if meta:
            from ..sources import media

            playable = media.browser_playable(p["video"], meta)
        return {
            "id": pid, "video": p["video"], "name": Path(p["video"]).name, "workdir": str(proj.root),
            "meta": meta, "src": run.src, "plan": run.plan(), "job": hub.status(pid),
            "config_files": run.config_files, "outputs": run.output_names(),
            "proxy": "direct" if playable else ("ready" if proj.media("proxy.mp4").exists() else proxy_jobs.get(pid, "none")),
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

    @app.get("/api/langs")
    def langs():
        return [{"code": p.code, "name": p.name, "annotations": list(p.annotations), "translit": p.translit_name,
                 "line_chars": p.line_chars, "word_based": p.word_based} for p in lang.available()]

    @app.get("/api/schema/config")
    def schema():
        return config_mod.Config.model_json_schema()

    # ---------- 项目 ----------
    @app.get("/api/projects")
    def list_projects():
        return [dict(p, name=Path(p["video"]).name, exists=os.path.isfile(p["video"])) for p in projects.all()]

    @app.post("/api/projects")
    def open_project(body: dict = Body(...)):
        entry = projects.add(body["path"], body.get("workdir"))
        return summary(entry["id"])

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
        hub.start(pid, run.run)
        return {"started": True}

    @app.get("/api/projects/{pid}/log")
    def get_log(pid: str):
        return hub.backlog.get(pid, [])

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
            "ocr": ocr, "asr": (res.get("asr") or {}).get("segments", []),
            "fused": {"items": fused.get("items", []), "stats": fused.get("stats")},
            "cues": cues, "tokens": tokens, "glossary": (res.get("translate") or {}).get("glossary"),
            "edits": load_json(run.proj / "edits.json", {}),
        }

    @app.get("/api/projects/{pid}/preview")
    def preview(pid: str, profile: str | None = None):
        """按输出方案排好的字幕（和导出的 ASS 同一套位置），界面直接叠在视频上。"""
        from ..render import ass, layout

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
            base = {"kind": p["kind"], "start": it["start"], "end": it["end"], "sources": it.get("sources", [])}
            if p["kind"] == "info":
                base.update(style=prof.info_style, lines=[layout.plain(l) for l in it["layers"][0][2]],
                            top=bool(p.get("top")), an=p.get("an"), x=p.get("x"), y=p.get("y"))
            else:
                base.update(margin_v=p["margin_v"], type=it.get("type"), rows=[
                    {"style": r["ly"].style, "role": r["ly"].role, "chunks": [[t, rt] for t, rt, _, _ in r["chunks"]],
                     "rsize": r["rsize"], "rcolor": r["ly"].annotate.color, "height": r["height"]} for r in p["rows"]])
            out.append(base)
        return {"profile": prof.name, "profiles": [x.name for x in profs], "px": px, "py": py,
                "styles": {k: v.model_dump() for k, v in used.items()}, "items": out}

    def _reannotate(pid):
        run = make_run(pid, until="annotate")
        run.run()
        run.load_state()
        return run

    @app.patch("/api/projects/{pid}/cues/{cid}")
    def edit_cue(pid: str, cid: str, body: dict = Body(...)):
        """改字幕：{"orig": "...", "tr": {"zh-Hans": "..."}, "start", "end", "type", "review", "delete"}"""
        _, proj = project_paths(pid)
        edits = load_json(proj / "edits.json", {})
        cur = edits.get(cid, {})
        for k, v in body.items():
            if k == "tr":
                cur.setdefault("tr", {}).update(v)
            else:
                cur[k] = v
        edits[cid] = cur
        save_json(proj / "edits.json", edits)
        run = _reannotate(pid)
        cues, tokens, _, _ = run.render_context()
        cue = next((c for c in cues if c["id"] == cid), None)
        return {"cue": cue, "tokens": tokens.get(cid), "edit": cur}

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
        alts = do(run.cfg, run.proj / "llm_cache", cues[i], cues[max(0, i - 3): i + 4], run.src, tgt, terms,
                  body.get("instruction", ""))
        return {"alts": alts, "lang": tgt}

    @app.post("/api/projects/{pid}/export")
    def export(pid: str, body: dict = Body(default={})):
        run = make_run(pid)
        names = run.export(body.get("profiles"))
        return {"files": names, "dir": str(Path(run.proj.video).parent)}

    # ---------- 媒体 ----------
    @app.get("/api/projects/{pid}/media/video")
    def video(pid: str):
        from ..sources import media

        p, proj = project_paths(pid)
        info = load_json(proj.stage("probe.json"))
        if info is None:
            info = media.probe(p["video"])
        if media.browser_playable(p["video"], info):
            return FileResponse(p["video"])
        proxy = proj.media("proxy.mp4")
        if proxy.exists():
            return FileResponse(proxy, media_type="video/mp4")
        if proxy_jobs.get(pid) != "building":
            proxy_jobs[pid] = "building"

            def build():
                try:
                    tmp = proj.media("proxy.tmp.mp4")
                    media.make_proxy(p["video"], tmp)
                    os.replace(tmp, proxy)
                    proxy_jobs.pop(pid, None)
                except Exception as e:
                    proxy_jobs[pid] = "failed"
                    env.log(f"生成预览视频失败：{e}")
            threading.Thread(target=build, daemon=True).start()
        return JSONResponse({"detail": "正在生成预览视频", "proxy": proxy_jobs[pid]}, status_code=202)

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
