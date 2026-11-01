"""作品的默认设置：新建这部作品的一集时复制一份过去，之后一集和作品各自独立；
可以用作品设置覆盖一集、用一集的设置覆盖作品（先看会变哪些项，被覆盖的那份先备份）。"""
import tomllib

from nyasub.core import config

from .test_server import client, opened  # noqa: F401


def _cfg(client, pid):
    return client.get(f"/api/projects/{pid}/config").json()


def _set_series(client, pid, name):
    project = _cfg(client, pid)["project"]
    project.setdefault("project", {})["series"] = name
    return client.put(f"/api/projects/{pid}/config", json={"project": project}).json()["config"]


def test_series_and_episode_are_independent(client, sample):
    pid, _ = opened(client, sample)
    cfg = _set_series(client, pid, "测试作品")
    assert cfg["series"] == {"name": "测试作品", "raw": {}, "effective": cfg["series"]["effective"]}

    # 用这一集的设置覆盖作品：先看会变哪些项
    pre = client.get(f"/api/projects/{pid}/series/overwrite", params={"direction": "to_series"}).json()
    assert pre["target"] == "series" and pre["changes"]
    before = cfg["effective"]
    r = client.post(f"/api/projects/{pid}/series/overwrite", json={"direction": "to_series"}).json()["config"]
    assert r["effective"] == before                       # 这一集不变
    assert "series" not in r["series"]["raw"].get("project", {})   # 作品设置里不记所属作品
    assert r["series"]["raw"].get("render") and config.has_series_settings("测试作品")

    # 改作品设置：这一集不受影响（两份各自独立）
    series = r["series"]["raw"]
    series.setdefault("ocr", {})["fps"] = 7.0
    r = client.put(f"/api/projects/{pid}/config", json={"series": series}).json()["config"]
    assert r["series"]["effective"]["ocr"]["fps"] == 7.0 and r["effective"]["ocr"]["fps"] == before["ocr"]["fps"]
    # 改这一集：作品不受影响
    p = r["project"]
    p.setdefault("ocr", {})["fps"] = 2.0
    p.setdefault("subs", {})["path"] = "这一集的字幕.srt"
    r = client.put(f"/api/projects/{pid}/config", json={"project": p}).json()["config"]
    assert r["effective"]["ocr"]["fps"] == 2.0 and r["series"]["effective"]["ocr"]["fps"] == 7.0

    # 用作品设置覆盖这一集：变化里有帧率；所属作品、外挂字幕文件留着；原来的设置备份了
    pre = client.get(f"/api/projects/{pid}/series/overwrite", params={"direction": "to_episode"}).json()
    assert {"path": "ocr.fps", "keys": ["ocr", "fps"], "before": 2.0, "after": 7.0} in pre["changes"]
    assert all(not c["path"].startswith(("project.series", "subs.path")) for c in pre["changes"])
    r = client.post(f"/api/projects/{pid}/series/overwrite", json={"direction": "to_episode"}).json()["config"]
    assert r["effective"]["ocr"]["fps"] == 7.0 and r["effective"]["project"]["series"] == "测试作品"
    assert r["effective"]["subs"]["path"] == "这一集的字幕.srt"
    proj = sample.with_name("sample.nyasub")
    assert any(proj.glob("backup/project-*.toml"))

    # 不合法的作品设置不会写进去
    assert client.put(f"/api/projects/{pid}/config", json={"series": {"ocr": {"fps": "很快"}}}).status_code == 400
    assert tomllib.loads(config.series_file("测试作品").read_text(encoding="utf-8"))["ocr"]["fps"] == 7.0


def test_new_episode_copies_series_defaults(client, sample):
    from nyasub import presets

    pid, _ = opened(client, sample)
    _set_series(client, pid, "测试作品")
    client.post(f"/api/projects/{pid}/series/overwrite", json={"direction": "to_series"})
    assert next(x for x in client.get("/api/series").json() if x["name"] == "测试作品")["settings"]
    cfg = presets.build("documentary", "ja", ["zh-Hans"], "测试作品", "第二集")
    assert cfg["project"]["series"] == "测试作品" and cfg["project"]["context"] == "第二集" and cfg.get("render")
    assert "render" in presets.build("documentary", "ja", ["zh-Hans"], "别的作品")


def test_series_needs_name(client, sample):
    pid, _ = opened(client, sample)
    assert client.put(f"/api/projects/{pid}/config", json={"series": {"ocr": {"fps": 2.0}}}).status_code == 400
    assert client.post(f"/api/projects/{pid}/series/overwrite", json={"direction": "to_series"}).status_code == 400


def test_autosave_backup_throttled(tmp_path):
    """自动保存时备份不刷屏：10 分钟内同类的只留第一份（改之前的样子）；别的备份照常每次都存。"""
    import os
    import time

    from nyasub.core import config

    f = tmp_path / "series.toml"
    f.write_text("a = 1", encoding="utf-8")
    for _ in range(3):
        config.write_backup(f, "old", every=600)
    baks = list((tmp_path / "backup").glob("series-*-old.toml"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == "a = 1"
    old = time.time() - 700
    os.utime(baks[0], (old, old))                          # 过了 10 分钟：再留一份
    time.sleep(1.1)
    config.write_backup(f, "old", every=600)
    assert len(list((tmp_path / "backup").glob("series-*-old.toml"))) == 2
    config.write_backup(f, "before-sync")
    assert len(list((tmp_path / "backup").glob("series-*.toml"))) == 3
