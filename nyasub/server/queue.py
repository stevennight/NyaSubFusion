"""批量队列：按顺序一个个处理，可以暂停 / 继续、调整顺序、移除、重试、取消。

- 存在 <用户数据目录>/queue.json，重启后恢复；上次中断时“运行中”的项改回“待处理”。
- 单个后台线程按顺序处理；事件同时推给 "_queue" 频道（队列页）和该项目自己的频道（流程页）。
- 取消：几秒内停下（语音识别、画面字识别、大模型请求、输出视频都在循环里检查）；已完成的步骤保留。
- 同一个项目正在别处运行时先跳过，稍后再试。
"""
import threading
import time
import uuid
from pathlib import Path

from .. import env
from ..core.pipeline import Cancelled
from ..core.store import load_json, save_json

ACTIVE = ("pending", "running")


class Queue:
    def __init__(self, hub, projects, make_run, setup_project, configured):
        self.hub = hub
        self.projects = projects
        self.make_run = make_run          # (pid, until, redo, translate) -> Run
        self.setup_project = setup_project  # (pid, options) -> None：按预设写 project.toml
        self.configured = configured      # (pid) -> bool
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.current = None               # (item_id, Run)
        self.path = env.home() / "queue.json"
        data = load_json(self.path, {"items": [], "paused": False})
        for it in data["items"]:
            if it["status"] == "running":
                it["status"], it["note"] = "pending", "上次运行被中断，重新开始"
        self.items, self.paused = data["items"], data.get("paused", False)
        self._save()
        threading.Thread(target=self._worker, daemon=True, name="nyasub-queue").start()

    # ---------- 状态 ----------
    def _save(self):
        save_json(self.path, {"items": self.items, "paused": self.paused})

    def state(self):
        with self.lock:
            return {"paused": self.paused, "items": [dict(i) for i in self.items],
                    "running": self.current[0] if self.current else None}

    def _changed(self):
        self._save()
        self.hub.publish("_queue", {"type": "queue", **self.state()})
        self.wake.set()

    def _find(self, item_id):
        it = next((i for i in self.items if i["id"] == item_id), None)
        if not it:
            raise KeyError(item_id)
        return it

    # ---------- 操作 ----------
    def add(self, videos, options):
        """options：{"until", "redo", "translate", "video", "preset", "source", "targets", "series", "context"}"""
        added = []
        with self.lock:
            for v in videos:
                entry = self.projects.add(v)
                if any(i["pid"] == entry["id"] and i["status"] in ACTIVE for i in self.items):
                    continue  # 已经在队列里
                it = {"id": uuid.uuid4().hex[:8], "pid": entry["id"], "video": entry["video"], "name": Path(entry["video"]).name,
                      "status": "pending", "options": options, "added": int(time.time()),
                      "stage": None, "error": None, "started": None, "finished": None, "note": None}
                self.items.append(it)
                added.append(it)
            self._changed()
        return added

    def remove(self, item_id):
        with self.lock:
            it = self._find(item_id)
            if it["status"] == "running":
                raise ValueError("正在运行，先取消")
            self.items.remove(it)
            self._changed()

    def move(self, item_id, delta):
        with self.lock:
            it = self._find(item_id)
            i = self.items.index(it)
            j = max(0, min(len(self.items) - 1, i + delta))
            self.items.insert(j, self.items.pop(i))
            self._changed()

    def retry(self, item_id):
        with self.lock:
            it = self._find(item_id)
            if it["status"] in ACTIVE:
                return
            it.update(status="pending", error=None, note=None, stage=None)
            self._changed()

    def cancel(self, item_id):
        with self.lock:
            it = self._find(item_id)
            if it["status"] == "pending":
                it["status"] = "cancelled"
            elif it["status"] == "running" and self.current and self.current[0] == item_id:
                it["note"] = "正在取消…"
                self.current[1].cancel.set()
            self._changed()

    def set_paused(self, paused):
        with self.lock:
            self.paused = bool(paused)
            self._changed()

    def clear_finished(self):
        with self.lock:
            self.items = [i for i in self.items if i["status"] in ACTIVE + ("failed",)]
            self._changed()

    # ---------- 后台处理 ----------
    def _next(self):
        with self.lock:
            if self.paused:
                return None
            for it in self.items:
                if it["status"] != "pending":
                    continue
                job = self.hub.jobs.get(it["pid"])
                if job and job.status == "running":
                    continue  # 这个项目在别处运行，先跳过
                return it
        return None

    def _worker(self):
        while True:
            it = self._next()
            if not it:
                self.wake.wait(timeout=2)
                self.wake.clear()
                continue
            self._run(it)

    def _run(self, it):
        pid, opts = it["pid"], it.get("options") or {}
        from .app import Job

        with self.lock:
            it.update(status="running", started=int(time.time()), error=None, stage=None)
            job = Job(pid)
            self.hub.jobs[pid] = job  # 项目页能看到“运行中”，也挡住同时的手动运行
            self.hub.backlog[pid] = []
            self._changed()
        tid = threading.get_ident()
        self.hub.threads[tid] = pid

        def relay(ev):  # 步骤和进度事件也推给队列页
            if threading.get_ident() != tid:
                return
            if ev.get("type") == "progress":
                self.hub.publish("_queue", {**ev, "type": "queue_progress", "item": it["id"]})
            if ev.get("type") == "stage":
                if ev.get("status") == "running":  # 只记真正开始运行的步骤（复用缓存、取消的不算）
                    with self.lock:
                        it["stage"] = ev.get("name")
                self.hub.publish("_queue", {**ev, "type": "queue_stage", "item": it["id"]})  # type 放后面，别被原事件覆盖
        env.on_event(relay)
        try:
            if not self.configured(pid):
                self.setup_project(pid, opts)
            run = self.make_run(pid, opts.get("until") or "render", opts.get("redo") or (), opts.get("translate"))
            run.cancel = job.cancel = threading.Event()  # 项目页的“停止”也能取消
            if opts.get("video"):  # 这一批要不要输出视频（覆盖项目配置）
                run.cfg.render.video.mode = opts["video"]
            with self.lock:
                self.current = (it["id"], run)
            run.run()
            status = "waiting" if run.waiting else "done"
            it["note"] = "翻译任务已导出，等外部助手完成后再运行一次" if run.waiting else None
        except Cancelled:
            status = "cancelled"
            it["note"] = "已取消"
        except Exception as e:
            status = "failed"
            it["error"] = str(e)[:500]
            env.log(f"队列：{it['name']} 失败：{e}")
        finally:
            env.off_event(relay)
            self.hub.threads.pop(tid, None)
            with self.lock:
                self.current = None
                it.update(status=status, finished=int(time.time()))
                job.status = "done" if status in ("done", "waiting") else status
                self.hub.publish(pid, {"type": "job", "status": job.status, "error": it.get("error")})
                self._changed()
