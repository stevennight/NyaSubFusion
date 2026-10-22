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
    assert all(st["status"] in ("missing", "off") for st in put["plan"])  # 语音 / 画面字关着的显示“未启用”
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
    assert [a["tr"] for a in r["alts"]] == ["甲", "乙", "丙"]  # 只给译文时原文保持不变
    orig = r["alts"][0]["orig"]
    # 重新分段：原文只动“｜”的位置；改了原文的字就不采用它的原文
    flat = orig.replace("\n", "")
    fakellm_alts["alts"] = [{"orig": flat[:4] + "｜" + flat[4:], "tr": "甲｜乙"}, {"orig": "别的字｜x", "tr": "丙｜丁"}]
    r = client.post(f"/api/projects/{pid}/cues/q1/retranslate", json={"instruction": "分段"}).json()
    assert r["alts"][0] == {"orig": flat[:4] + "｜" + flat[4:], "tr": "甲｜乙"}
    assert r["alts"][1] == {"orig": orig, "tr": "丙 丁"}

    # 用户手动改过原文：重译以改后的原文为准，来源只当时间和语境参考
    seen = {}

    def call(self, s, u, images=None):
        seen["system"], seen["user"] = s, u
        return {"alts": ["甲"]}
    llm_client.LLM._call = call
    client.post(f"/api/projects/{pid}/cues/q1/retranslate", json={"instruction": "不改原文时"})
    assert "手动改过" not in seen["system"] and "## 本条的来源" in seen["user"]
    client.patch(f"/api/projects/{pid}/cues/q1", json={"orig": "改过的原文"})
    client.post(f"/api/projects/{pid}/cues/q1/retranslate", json={"instruction": "改了原文后"})
    assert "原文是用户手动改过的" in seen["system"] and "改过的原文" in seen["user"]
    assert "## 本条改之前的来源（只看时间和语境，内容以原文为准）" in seen["user"]


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


def test_split_merge_insert_undo(client, sample):
    pid, _ = opened(client, sample)
    client.post(f"/api/projects/{pid}/run", json={})
    wait_job(client, pid)
    ids = lambda: [c["id"] for c in client.get(f"/api/projects/{pid}/data").json()["cues"]]
    assert ids() == ["q1", "q2", "q3", "q4"]

    r = client.post(f"/api/projects/{pid}/cues/q1/split", json={"at": 4.0})
    assert r.status_code == 200 and r.json()["cue"]["id"] == "q1a"
    assert ids() == ["q1a", "q1b", "q2", "q3", "q4"]
    assert client.post(f"/api/projects/{pid}/cues/q1/split", json={"at": 4.0}).status_code == 404  # q1 已经不存在
    assert client.post(f"/api/projects/{pid}/cues/q2/split", json={"at": 0.1}).status_code == 400

    client.post(f"/api/projects/{pid}/cues/q3/merge")
    assert ids() == ["q1a", "q1b", "q2", "q3"]
    r = client.post(f"/api/projects/{pid}/cues", json={"start": 36, "end": 38, "orig": "追加のセリフ"}).json()
    assert r["cue"]["id"] == "u1" and r["history"]["undo"] == 3
    # 插入的条目也会被注音、出现在预览里
    assert client.get(f"/api/projects/{pid}/data").json()["tokens"]["u1"]["orig"]

    client.post(f"/api/projects/{pid}/edits/undo")
    client.post(f"/api/projects/{pid}/edits/undo")
    assert ids() == ["q1a", "q1b", "q2", "q3", "q4"]
    client.post(f"/api/projects/{pid}/edits/redo")
    assert ids() == ["q1a", "q1b", "q2", "q3"]
    for _ in range(2):
        client.post(f"/api/projects/{pid}/edits/undo")
    assert ids() == ["q1", "q2", "q3", "q4"]
    assert client.post(f"/api/projects/{pid}/edits/undo").status_code == 400


def test_frame_jpeg(client, sample):
    pid, _ = opened(client, sample)
    r = client.get(f"/api/projects/{pid}/media/frame", params={"t": 9.5, "h": 180})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"


def test_glossary_and_readings_api(client, sample, isolated_home):
    pid, _ = opened(client, sample)
    client.post(f"/api/projects/{pid}/run", json={})
    wait_job(client, pid)
    g = client.get(f"/api/projects/{pid}/glossary").json()
    assert g["series"] == "" and g["terms"][0]["src"] == "深浦町" and g["terms"][0]["from"] == "sample"
    r = client.put(f"/api/projects/{pid}/glossary", json={"terms": g["terms"] + [{"src": "青森県", "tr": "青森县"}, {"src": " "}]}).json()
    assert [t["src"] for t in r["terms"]] == ["深浦町", "青森県"] and r["terms"][1]["from"] == "manual"
    assert {p["stage"]: p["status"] for p in r["plan"]}["translate"] == "stale"  # 术语改了要重新翻译

    rd = client.put(f"/api/projects/{pid}/readings", json={"global": [{"lang": "ja", "src": "深浦町", "reading": "ふかうらまち"}]}).json()
    assert rd["global"][0]["reading"] == "ふかうらまち" and (isolated_home / "readings.json").exists()
    assert client.get("/api/series").json() == []


def test_open_wizard_and_presets(client, sample):
    s = client.post("/api/projects", json={"path": str(sample)}).json()
    assert s["configured"] is False
    assert s["probe"]["has_audio"] and s["probe"]["external_subs"].endswith("sample.srt")
    ids = [p["id"] for p in client.get("/api/presets").json()]
    assert ids[0] == "documentary" and "lecture" in ids
    r = client.post(f"/api/projects/{s['id']}/setup", json={"preset": "lecture", "source": "ja", "targets": ["zh-Hans", "en"], "series": "测试"})
    assert r.status_code == 200, r.text
    eff = r.json()["config"]["effective"]
    assert eff["ocr"]["enabled"] is False and eff["project"]["targets"] == ["zh-Hans", "en"] and eff["project"]["preset"] == "lecture"
    assert eff["render"]["profiles"][0]["format"] == "srt"
    assert client.post("/api/projects", json={"path": str(sample)}).json()["configured"] is True


def test_edit_never_reruns_translation(client, sample):
    """上游配置改了（翻译过期）时编辑字幕，只重做注音，不会调大模型。"""
    pid, _ = opened(client, sample)
    client.post(f"/api/projects/{pid}/run", json={})
    wait_job(client, pid)
    client.put(f"/api/projects/{pid}/config", json={"project": dict(PROJECT, project={"context": "改了说明"})})
    assert {p["stage"]: p["status"] for p in client.get(f"/api/projects/{pid}/plan").json()}["translate"] == "stale"
    calls = len(fakellm.CALLS)
    r = client.patch(f"/api/projects/{pid}/cues/q1", json={"tr": {"zh-Hans": "改字"}})
    assert r.status_code == 200 and r.json()["cue"]["tr"]["zh-Hans"]["text"] == "改字"
    assert client.post(f"/api/projects/{pid}/cues/q2/split", json={"at": 14.0}).status_code == 200
    assert len(fakellm.CALLS) == calls


def test_project_list_details_and_forget(client, sample):
    pid, _ = opened(client, sample)
    client.post(f"/api/projects/{pid}/run", json={})
    assert wait_job(client, pid)["status"] == "done"
    p = next(x for x in client.get("/api/projects").json() if x["id"] == pid)
    assert p["folder"] == sample.parent.name and p["duration"] == pytest.approx(40, abs=1)
    assert p["configured"] and p["reached"] and "sample.zh-Hans.ass" in p["outputs"] and not p["running"]
    assert all("-merged." not in f for f in p["outputs"])
    left = client.delete(f"/api/projects/{pid}").json()
    assert all(x["id"] != pid for x in left) and sample.exists()  # 只从列表去掉，文件还在
