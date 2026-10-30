"""模板：全局配置只留跟电脑有关的项、自己的预设、一集和模板比较 / 逐项恢复、作品默认设置同步给已有的集。"""
import shutil
import tomllib

from nyasub import presets
from nyasub.core import config

from .test_server import client, opened, wait_job  # noqa: F401


def test_global_processing_keys_move_to_preset(tmp_path, isolated_home):
    (isolated_home / "config.toml").write_text(
        '[llm]\ntranslate = "ds"\nproviders = [{name = "ds", base_url = "http://x/v1"}]\n'
        '[asr]\nmodel = "small"\nhf_endpoint = "https://mirror"\n[render.styles.info]\nsize = 50\n', encoding="utf-8")
    old = tmp_path / "old" / "project.toml"
    old.parent.mkdir()
    old.write_text('[project]\nsource = "ja"\n', encoding="utf-8")
    cfg, _ = config.load(None, old)
    # 旧项目结果不变：以前从全局继承的照样有
    assert cfg.asr.model == "small" and cfg.render.styles["info"].size == 50 and cfg.llm.translate == "ds"
    assert tomllib.loads(old.read_text(encoding="utf-8"))["asr"]["model"] == "small"
    # 全局配置只剩跟电脑有关的
    g = tomllib.loads((isolated_home / "config.toml").read_text(encoding="utf-8"))
    assert g == {"llm": {"translate": "ds", "providers": [{"name": "ds", "base_url": "http://x/v1"}]}, "asr": {"hf_endpoint": "https://mirror"}}
    # 挪到“我的默认”预设，新项目默认用它
    assert presets.default_id() == presets.USER + presets.LEGACY_NAME
    assert presets.read_user(presets.LEGACY_NAME)["asr"]["model"] == "small"
    new, files = config.load(None, tmp_path / "new" / "project.toml")
    assert new.asr.model == "small" and any("我的默认" in f for f in files)
    # 以后改全局配置里的处理参数：旧项目不受影响（又挪进预设）
    (isolated_home / "config.toml").write_text('[asr]\nmodel = "medium"\n', encoding="utf-8")
    assert config.load(None, old)[0].asr.model == "small"
    assert presets.read_user(presets.LEGACY_NAME)["asr"]["model"] == "medium"


def test_episode_template_diff_and_restore(client, sample):
    pid, _ = opened(client, sample)
    client.post(f"/api/projects/{pid}/setup", json={"preset": "documentary", "source": "ja", "targets": ["zh-Hans"]})
    t = client.get(f"/api/projects/{pid}/template").json()
    assert t["kind"] == "preset" and t["id"] == "documentary" and not t["editable"] and t["changes"] == []
    proj = client.get(f"/api/projects/{pid}/config").json()["project"]
    proj["ocr"]["fps"] = 5.0
    proj["render"]["styles"]["sub"]["size"] = 44
    client.put(f"/api/projects/{pid}/config", json={"project": proj})
    t = client.get(f"/api/projects/{pid}/template").json()
    assert {(tuple(c["keys"]), c["before"], c["after"]) for c in t["changes"]} == {
        (("ocr", "fps"), 5.0, 3.0), (("render", "styles", "sub", "size"), 44, 38)}
    # 内置预设不能改
    assert client.post(f"/api/projects/{pid}/template/apply", json={"keys": [["ocr", "fps"]], "to": "template"}).status_code == 400
    r = client.post(f"/api/projects/{pid}/template/apply", json={"keys": [["ocr", "fps"]], "to": "episode"}).json()
    assert r["config"]["effective"]["ocr"]["fps"] == 3.0 and r["config"]["effective"]["render"]["styles"]["sub"]["size"] == 44
    assert list(sample.with_name("sample.nyasub").glob("backup/project-*-before-template.toml"))


def test_user_presets(client, sample):
    pid, _ = opened(client, sample)
    proj = client.get(f"/api/projects/{pid}/config").json()["project"]
    proj.setdefault("project", {}).update(series="作品A", context="这一集的说明")
    proj["ocr"]["fps"] = 6.0
    client.put(f"/api/projects/{pid}/config", json={"project": proj})
    r = client.post("/api/presets", json={"name": "我的纪录片", "from_pid": pid}).json()
    assert r["id"] == "user:我的纪录片" and any(p["id"] == r["id"] and not p["builtin"] for p in r["list"])
    p = client.get("/api/presets/user:我的纪录片").json()
    assert p["raw"]["ocr"]["fps"] == 6.0 and "series" not in p["raw"].get("project", {}) and p["raw"]["render"]["styles"]
    assert client.post("/api/presets", json={"name": "我的纪录片"}).status_code == 400
    assert client.post("/api/presets", json={"name": "a/b"}).status_code == 400
    p["raw"]["ocr"]["fps"] = 2.5
    assert client.put("/api/presets/user:我的纪录片", json={"raw": p["raw"]}).json()["effective"]["ocr"]["fps"] == 2.5
    assert client.put("/api/presets/documentary", json={"raw": {}}).status_code == 400
    lst = client.post("/api/presets/user:我的纪录片/default").json()
    assert next(x for x in lst if x["default"])["id"] == "user:我的纪录片"
    # 用这个预设建的一集，它就是模板，而且能存回去
    client.post(f"/api/projects/{pid}/setup", json={"preset": "user:我的纪录片", "source": "ja", "targets": ["zh-Hans"]})
    t = client.get(f"/api/projects/{pid}/template").json()
    assert t["kind"] == "preset" and t["editable"] and t["changes"] == []
    proj = client.get(f"/api/projects/{pid}/config").json()["project"]
    proj["ocr"]["fps"] = 8.0
    client.put(f"/api/projects/{pid}/config", json={"project": proj})
    client.post(f"/api/projects/{pid}/template/apply", json={"keys": [["ocr", "fps"]], "to": "template"})
    assert presets.read_user("我的纪录片")["ocr"]["fps"] == 8.0
    assert not any(x["id"] == "user:我的纪录片" for x in client.delete("/api/presets/user:我的纪录片").json())


def test_series_sync_to_episodes(client, sample):
    second = sample.with_name("second.mp4")
    shutil.copy(sample, second)
    a, _ = opened(client, sample)
    b, _ = opened(client, second)
    for pid in (a, b):
        proj = client.get(f"/api/projects/{pid}/config").json()["project"]
        proj.setdefault("project", {})["series"] = "作品B"
        client.put(f"/api/projects/{pid}/config", json={"project": proj})
    raw = client.get(f"/api/projects/{a}/config").json()["project"]
    s = client.put("/api/series/作品B", json={"raw": raw}).json()
    assert s["has_settings"] and "series" not in s["raw"].get("project", {}) and sorted(e["diff"] for e in s["episodes"]) == [0, 0]
    raw = s["raw"]
    raw["ocr"]["fps"] = 9.0
    s = client.put("/api/series/作品B", json={"raw": raw}).json()
    assert sorted(e["diff"] for e in s["episodes"]) == [1, 1]   # 改作品默认设置不影响已有的集
    vals = client.post("/api/series/作品B/values", json={"keys": [["ocr", "fps"]]}).json()
    assert {v["pid"]: v["values"] for v in vals} == {a: [3.0], b: [3.0]}
    r = client.post("/api/series/作品B/sync", json={"keys": [["ocr", "fps"]], "pids": [a]}).json()
    assert r["synced"] == [a]
    eff = {pid: client.get(f"/api/projects/{pid}/config").json()["effective"]["ocr"]["fps"] for pid in (a, b)}
    assert eff == {a: 9.0, b: 3.0}
    assert client.get(f"/api/projects/{a}/template").json()["kind"] == "series"


def test_restore_last_run(client, sample):
    pid, _ = opened(client, sample)
    assert client.post(f"/api/projects/{pid}/config/last_run").status_code == 400
    client.post(f"/api/projects/{pid}/run", json={})
    wait_job(client, pid)
    proj = client.get(f"/api/projects/{pid}/config").json()["project"]
    fps = proj["ocr"].get("fps", 3.0)
    proj["ocr"]["fps"] = 7.5
    client.put(f"/api/projects/{pid}/config", json={"project": proj})
    r = client.post(f"/api/projects/{pid}/config/last_run").json()
    assert r["config"]["effective"]["ocr"]["fps"] == fps
