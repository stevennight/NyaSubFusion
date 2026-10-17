"""本地服务接口（M3）。"""
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from nyasub.server.app import create_app  # noqa: E402

from . import fakellm  # noqa: E402

PROJECT = {
    "llm": {"mode": "api", "model": "fake", "api_key_env": "NYASUB_TEST_KEY"},
    "asr": {"enabled": False},
    "ocr": {"enabled": False},
    "render": {"profiles": [
        {"name": "bilingual", "layers": [{"role": "translation", "style": "main"},
                                          {"role": "original", "show": "auto", "style": "sub"}]},
        {"name": "learn", "filename": "{stem}.learn.{ext}",
         "layers": [{"role": "original", "style": "orig_main", "annotate": {"kind": "furigana", "scope": "all"}}]},
    ]},
}


@pytest.fixture
def client(monkeypatch):
    fakellm.install(monkeypatch)
    with TestClient(create_app()) as c:
        yield c


def wait_job(client, pid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = client.get(f"/api/projects/{pid}").json()["job"]
        if job and job["status"] != "running":
            return job
        time.sleep(0.2)
    raise TimeoutError


def opened(client, sample):
    s = client.post("/api/projects", json={"path": str(sample)}).json()
    pid = s["id"]
    r = client.put(f"/api/projects/{pid}/config", json={"project": PROJECT})
    assert r.status_code == 200, r.text
    return pid, r.json()


def test_open_run_and_data(client, sample):
    pid, put = opened(client, sample)
    assert all(st["status"] == "missing" for st in put["plan"])
    assert [p["id"] for p in client.get("/api/projects").json()] == [pid]

    with client.websocket_connect(f"/ws/projects/{pid}") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert client.post(f"/api/projects/{pid}/run", json={}).json()["started"]
        seen = []
        while True:
            ev = ws.receive_json()
            seen.append(ev)
            if ev["type"] == "job":
                break
    assert ev["status"] == "done"
    assert any(e["type"] == "stage" and e["name"] == "translate" and e["status"] == "done" for e in seen)
    assert any(e["type"] == "log" for e in seen)

    s = client.get(f"/api/projects/{pid}").json()
    assert {st["stage"]: st["status"] for st in s["plan"]}["annotate"] == "done"
    assert s["proxy"] == "direct"  # 样片是 H.264 mp4，浏览器能直接播

    data = client.get(f"/api/projects/{pid}/data").json()
    assert data["src"] == "ja" and len(data["cues"]) == 4 and len(data["subs"]) == 4
    assert data["tokens"]["q1"]["orig"]

    prev = client.get(f"/api/projects/{pid}/preview", params={"profile": "learn"}).json()
    assert prev["profile"] == "learn" and prev["py"] == 1080
    row = prev["items"][0]["rows"][0]
    assert any(rt for _, rt in row["chunks"]) and prev["items"][0]["margin_v"] >= 46


def test_config_change_marks_stale(client, sample):
    pid, _ = opened(client, sample)
    client.post(f"/api/projects/{pid}/run", json={})
    assert wait_job(client, pid)["status"] == "done"
    changed = dict(PROJECT, project={"context": "换一个节目说明"})
    plan = {st["stage"]: st["status"] for st in client.put(f"/api/projects/{pid}/config", json={"project": changed}).json()["plan"]}
    assert plan["fuse"] == "done" and plan["translate"] == "stale" and plan["annotate"] == "stale"
    bad = client.put(f"/api/projects/{pid}/config", json={"project": {"asr": {"device": "tpu"}}})
    assert bad.status_code == 400


def test_edit_reading_export(client, sample, isolated_home):
    pid, _ = opened(client, sample)
    client.post(f"/api/projects/{pid}/run", json={})
    wait_job(client, pid)

    r = client.patch(f"/api/projects/{pid}/cues/q1", json={"tr": {"zh-Hans": "青森县西端"}, "review": "ok"}).json()
    assert r["cue"]["tr"]["zh-Hans"]["text"] == "青森县西端" and r["cue"]["review"] == "ok"
    assert r["tokens"]["tr"]["zh-Hans"]  # 改过的译文重新注了音

    r = client.post(f"/api/projects/{pid}/readings", json={"lang": "ja", "src": "深浦町", "reading": "ふかうらちょう"})
    assert r.status_code == 200 and (isolated_home / "readings.json").exists()
    prev = client.get(f"/api/projects/{pid}/preview", params={"profile": "learn"}).json()
    assert any(rt == "ふかうらちょう" for it in prev["items"] for row in it.get("rows", []) for _, rt in row["chunks"])

    files = client.post(f"/api/projects/{pid}/export", json={"profiles": ["learn"]}).json()["files"]
    assert files == ["sample.learn.ass"] and sample.with_name("sample.learn.ass").exists()


def test_media_and_retranslate(client, sample):
    pid, _ = opened(client, sample)
    client.post(f"/api/projects/{pid}/run", json={})
    wait_job(client, pid)
    r = client.get(f"/api/projects/{pid}/media/video", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    peaks = client.get(f"/api/projects/{pid}/media/peaks").json()
    assert len(peaks["peaks"]) > 1000

    fakellm_alts = {"alts": ["甲", "乙", "丙"]}
    from nyasub.llm import client as llm_client

    llm_client.LLM._call = lambda self, s, u, images=None: fakellm_alts
    r = client.post(f"/api/projects/{pid}/cues/q1/retranslate", json={"instruction": "更口语"}).json()
    assert r["alts"] == ["甲", "乙", "丙"]


def test_token_required(sample):
    with TestClient(create_app("secret")) as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/health", headers={"x-nyasub-token": "secret"}).status_code == 200


def test_shutdown_closes_websockets(client):
    with client.websocket_connect("/ws/projects/_components") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert client.post("/api/shutdown").json()["ok"]
        with pytest.raises(Exception):  # 服务端主动关闭了连接
            ws.receive_json()
