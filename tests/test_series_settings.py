"""同一作品的各集共用设置：作品设置（series.toml）在全局之后、这一集之前；可以把一集的设置设为作品默认；
新建这部作品的项目直接跟随作品设置。"""
import tomllib

from nyasub.core import config

from .test_server import client, opened  # noqa: F401


def _cfg(client, pid):
    return client.get(f"/api/projects/{pid}/config").json()


def test_series_settings_shared(client, sample):
    pid, _ = opened(client, sample)
    cfg = _cfg(client, pid)
    project = cfg["project"]
    project.setdefault("project", {})["series"] = "测试作品"
    client.put(f"/api/projects/{pid}/config", json={"project": project})
    cfg = _cfg(client, pid)
    assert cfg["series"]["name"] == "测试作品" and cfg["series"]["raw"] == {}

    # 把这一集的设置设为作品默认：实际用的设置不变，这一集只留下只属于它的项
    before = cfg["effective"]
    r = client.post(f"/api/projects/{pid}/series/adopt").json()["config"]
    assert r["effective"] == before
    assert set(r["project"]) <= {"project", "subs"} and r["project"]["project"]["series"] == "测试作品"
    assert r["series"]["raw"].get("render") and config.has_series_settings("测试作品")

    # 改作品设置：这一集跟着变
    series = r["series"]["raw"]
    series.setdefault("ocr", {})["fps"] = 7.0
    r = client.put(f"/api/projects/{pid}/config", json={"series": series}).json()["config"]
    assert r["effective"]["ocr"]["fps"] == 7.0 and r["series"]["effective"]["ocr"]["fps"] == 7.0
    # 这一集单独设的项优先；“全部跟随作品”清掉它
    p = r["project"]
    p.setdefault("ocr", {})["fps"] = 2.0
    r = client.put(f"/api/projects/{pid}/config", json={"project": p}).json()["config"]
    assert r["effective"]["ocr"]["fps"] == 2.0
    r = client.post(f"/api/projects/{pid}/series/adopt", json={"mode": "follow"}).json()["config"]
    assert r["effective"]["ocr"]["fps"] == 7.0 and "ocr" not in r["project"]

    # 不合法的作品设置不会写进去
    assert client.put(f"/api/projects/{pid}/config", json={"series": {"ocr": {"fps": "很快"}}}).status_code == 400
    assert tomllib.loads(config.series_file("测试作品").read_text(encoding="utf-8"))["ocr"]["fps"] == 7.0

    # 作品列表标出有共享设置的；新建这部作品的项目只记下作品，其他跟随
    assert next(x for x in client.get("/api/series").json() if x["name"] == "测试作品")["settings"]
    from nyasub import presets

    assert presets.build("documentary", "ja", ["zh-Hans"], "测试作品", "第二集") == \
        {"project": {"series": "测试作品", "context": "第二集"}}
    assert "render" in presets.build("documentary", "ja", ["zh-Hans"], "别的作品")


def test_series_needs_name(client, sample):
    pid, _ = opened(client, sample)
    assert client.put(f"/api/projects/{pid}/config", json={"series": {"ocr": {"fps": 2.0}}}).status_code == 400
    assert client.post(f"/api/projects/{pid}/series/adopt").status_code == 400
