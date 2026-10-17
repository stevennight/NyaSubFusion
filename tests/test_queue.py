"""批量队列：顺序处理、暂停 / 继续、调整顺序、取消 / 重试 / 移除、重启恢复。"""
import json
import shutil
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from nyasub.server.app import create_app  # noqa: E402

from . import fakellm  # noqa: E402
from .conftest import FIXTURES  # noqa: E402
from .test_server import PROJECT  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    fakellm.install(monkeypatch)
    with TestClient(create_app()) as c:
        yield c


def _videos(tmp_path, n):
    out = []
    for i in range(n):
        d = tmp_path / f"ep{i + 1}"
        d.mkdir()
        for name in ("sample.mp4", "sample.srt"):
            shutil.copy(FIXTURES / name, d / name)
        out.append(d / "sample.mp4")
    return out


def _configure(client, video):
    pid = client.post("/api/projects", json={"path": str(video)}).json()["id"]
    assert client.put(f"/api/projects/{pid}/config", json={"project": PROJECT}).status_code == 200
    return pid


def _wait(client, cond, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = client.get("/api/queue").json()
        if cond(st):
            return st
        time.sleep(0.2)
    raise TimeoutError(client.get("/api/queue").json())


def test_queue_order_pause_and_finish(client, tmp_path):
    v1, v2 = _videos(tmp_path, 2)
    p1, p2 = _configure(client, v1), _configure(client, v2)
    client.post("/api/queue/pause", json={"paused": True})
    st = client.post("/api/queue", json={"paths": [str(v1), str(v2)]}).json()
    assert st["added"] == 2 and [i["pid"] for i in st["items"]] == [p1, p2]
    assert client.post("/api/queue", json={"paths": [str(v1)]}).json()["added"] == 0  # 已经在队列里
    second = st["items"][1]["id"]
    client.post(f"/api/queue/{second}/up")
    assert [i["pid"] for i in client.get("/api/queue").json()["items"]] == [p2, p1]
    time.sleep(0.5)
    assert all(i["status"] == "pending" for i in client.get("/api/queue").json()["items"])  # 暂停中不处理

    client.post("/api/queue/pause", json={"paused": False})
    st = _wait(client, lambda s: all(i["status"] == "done" for i in s["items"]))
    a, b = st["items"]
    assert a["pid"] == p2 and a["finished"] <= b["started"]  # 按调整后的顺序一个个跑
    assert v1.with_name("sample.zh-Hans.ass").exists() and v2.with_name("sample.zh-Hans.ass").exists()
    assert client.get(f"/api/projects/{p1}").json()["job"]["status"] == "done"
    assert client.post("/api/queue/clear").json()["items"] == []


def test_unconfigured_video_uses_options(client, tmp_path):
    (v,) = _videos(tmp_path, 1)
    client.post("/api/queue/pause", json={"paused": True})
    pid = client.post("/api/queue", json={"paths": [str(v.parent)], "options": {"preset": "lecture", "targets": ["en"], "until": "subs"}}).json()["items"][0]["pid"]
    client.post("/api/queue/pause", json={"paused": False})
    _wait(client, lambda s: s["items"][0]["status"] == "done")
    eff = client.get(f"/api/projects/{pid}/config").json()["effective"]
    assert eff["project"]["preset"] == "lecture" and eff["project"]["targets"] == ["en"] and eff["ocr"]["enabled"] is False


def test_cancel_retry_remove_and_bad_paths(client, tmp_path):
    (v,) = _videos(tmp_path, 1)
    _configure(client, v)
    client.post("/api/queue/pause", json={"paused": True})
    item = client.post("/api/queue", json={"paths": [str(v)]}).json()["items"][0]["id"]
    assert client.post(f"/api/queue/{item}/cancel").json()["items"][0]["status"] == "cancelled"
    assert client.post(f"/api/queue/{item}/retry").json()["items"][0]["status"] == "pending"
    assert client.post(f"/api/queue/{item}/remove").json()["items"] == []
    assert client.post("/api/queue/nope/remove").status_code == 404
    empty = tmp_path / "empty"
    empty.mkdir()
    assert client.post("/api/queue", json={"paths": [str(empty)]}).status_code == 400


def test_restart_recovers_running_items(isolated_home, sample):
    (isolated_home / "queue.json").write_text(json.dumps({"paused": True, "items": [
        {"id": "x1", "pid": "p", "video": str(sample), "name": "sample.mp4", "status": "running", "options": {}}]}), encoding="utf-8")
    with TestClient(create_app()) as c:
        st = c.get("/api/queue").json()
        assert st["paused"] and st["items"][0]["status"] == "pending" and "中断" in st["items"][0]["note"]


def test_stage_events_reach_queue_channel(client, tmp_path):
    (v,) = _videos(tmp_path, 1)
    _configure(client, v)
    with client.websocket_connect("/ws/projects/_queue") as ws:
        ws.receive_json()
        client.post("/api/queue", json={"paths": [str(v)]})
        types = set()
        while True:
            ev = ws.receive_json()
            types.add(ev["type"])
            if ev["type"] == "queue_stage":
                assert ev["item"] and ev["name"]
            if ev["type"] == "queue" and ev["items"][0]["status"] == "done":
                break
    assert "queue_stage" in types and "stage" not in types


def test_cancel_running_item_stops_between_stages(client, tmp_path, monkeypatch):
    """取消正在运行的项：当前步骤做完后停下，状态为已取消，队列接着处理下一项。"""
    import threading

    from nyasub.core import pipeline

    v1, v2 = _videos(tmp_path, 2)
    _configure(client, v1)
    _configure(client, v2)
    gate = threading.Event()
    orig = pipeline.Run._load_subs

    def slow_subs(self, info):  # 让“自带字幕”这一步卡住，好在运行中取消
        if "ep1" in str(self.proj.video):
            gate.wait(10)
        return orig(self, info)
    monkeypatch.setattr(pipeline.Run, "_load_subs", slow_subs)
    st = client.post("/api/queue", json={"paths": [str(v1), str(v2)]}).json()
    first = st["items"][0]["id"]
    _wait(client, lambda s: s["items"][0]["status"] == "running" and s["items"][0]["stage"] == "subs")
    client.post(f"/api/queue/{first}/cancel")
    gate.set()
    st = _wait(client, lambda s: s["items"][1]["status"] == "done")
    assert st["items"][0]["status"] == "cancelled"
    assert not v1.with_name("sample.zh-Hans.ass").exists()  # 停在了“自带字幕”之后
