"""样式是整套的（配置格式 2）：能删、能指定兜底样式、能恢复出厂；旧项目第一次读的时候升级。"""
import tomllib

from nyasub.core import config
from nyasub.render import ass

from .test_server import client, opened  # noqa: F401


def test_old_project_upgrades_with_backup(tmp_path, isolated_home):
    (isolated_home / "config.toml").write_text("[render.styles.info]\nsize = 50\n", encoding="utf-8")
    pf = tmp_path / "project.toml"
    pf.write_text("[render.styles.sub]\nsize = 30\n\n[render.styles.my]\nfont = \"KaiTi\"\n", encoding="utf-8")
    cfg, _ = config.load(None, pf)
    raw = tomllib.loads(pf.read_text(encoding="utf-8"))
    assert raw["version"] == 2 and set(raw["render"]["styles"]) == {"main", "sub", "orig_main", "info", "translit", "my"}
    # 升级前合并出来的样子不变：项目改的、全局改的、出厂的都在
    assert cfg.render.styles["sub"].size == 30 and cfg.render.styles["sub"].font == "Yu Gothic"
    assert cfg.render.styles["info"].size == 50 and cfg.render.styles["my"].font == "KaiTi"
    assert list((tmp_path / "backup").glob("project-*-v1.toml"))
    # 升级以后全局的样式不再影响这一集
    (isolated_home / "config.toml").write_text("[render.styles.info]\nsize = 20\n", encoding="utf-8")
    assert config.load(None, pf)[0].render.styles["info"].size == 50


def test_own_style_set_can_delete_and_fill_from_factory(tmp_path, isolated_home):
    pf = tmp_path / "project.toml"
    pf.write_text('version = 2\nrender.fallback_style = "sub"\n'
                  '[render.styles.sub]\nsize = 30\n[render.styles."正文"]\nbase = "main"\ncolor = "#FF0000"\n', encoding="utf-8")
    cfg, _ = config.load(None, pf)
    assert set(cfg.render.styles) == {"sub", "正文"}          # 删掉的出厂样式不会自己回来
    assert cfg.render.styles["正文"].font == "Microsoft YaHei" and cfg.render.styles["正文"].size == 60  # 按 base 补齐
    assert cfg.render.style("main").size == 30                # 不存在的样式用兜底样式


def test_place_uses_fallback_for_missing_style():
    cfg = config.Config.model_validate({"version": 2, "render": {"fallback_style": "sub", "styles": {"sub": {}}}})
    prof = config.ProfileCfg(name="x", layers=[config.LayerCfg(role="translation", style="没有这个")])
    item = {"start": 0, "end": 1, "kind": "main", "layers": [(prof.layers[0], "zh-Hans", [[("你好", None)]])]}
    _, _, used, _ = ass.place([item], prof, cfg.render.styles, 16 / 9, [], fallback="sub")
    assert used["没有这个"].size == 38


def test_style_line_box_and_spacing():
    st = config.StyleCfg(box=True, box_color="#102030", box_opacity=0.5, outline=8, spacing=2)
    parts = ass.style_line("x", st).split(",")
    assert parts[5] == "&H80302010" and parts[13] == "2" and parts[15] == "3" and parts[16] == "8"
    assert ass.style_line("y", config.StyleCfg()).split(",")[15] == "1"


def test_bad_style_name_rejected(client, sample):
    pid, _ = opened(client, sample)
    proj = client.get(f"/api/projects/{pid}/config").json()["project"]
    proj["render"]["styles"]["a,b"] = {}
    assert client.put(f"/api/projects/{pid}/config", json={"project": proj}).status_code == 400


def test_saved_project_owns_full_style_set(client, sample):
    pid, _ = opened(client, sample)
    r = client.get(f"/api/projects/{pid}/config").json()
    assert r["project"]["version"] == 2 and "translit" in r["project"]["render"]["styles"]
    assert set(r["factory"]["styles"]) == set(config.DEFAULT_STYLES)
    proj = r["project"]
    del proj["render"]["styles"]["translit"]
    r = client.put(f"/api/projects/{pid}/config", json={"project": proj}).json()["config"]
    assert "translit" not in r["effective"]["render"]["styles"]
