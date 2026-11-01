"""内容预设：按节目类型打包的推荐配置，新建项目时写进 project.toml（之后都可以再改）。

内置预设写在这里，只读；自己的预设存在 用户目录/presets/<名字>.toml（一份完整的设置，和作品默认设置一样），
其中一个可以设成“新项目默认用它”（presets/index.json）。不属于作品的一集，建它时选的预设就是它的模板。
"""
import copy
import re

from . import env

_BILINGUAL = {"name": "bilingual", "filename": "{stem}.{tr}.{ext}", "layers": [
    {"role": "translation", "style": "main"},
    {"role": "original", "show": "auto", "style": "sub"},
]}
_SRT = {"name": "translation", "format": "srt", "filename": "{stem}.{tr}.{ext}", "info_mode": "top",
        "layers": [{"role": "translation", "style": "main"}]}
_LEARN = {"name": "learn", "filename": "{stem}.learn.{ext}", "layers": [
    {"role": "original", "style": "orig_main", "annotate": {"kind": "furigana", "scope": "all"}},
    {"role": "transliteration", "of": "original", "style": "translit"},
    {"role": "translation", "style": "sub"},
]}

PRESETS = {
    "documentary": {
        "name": "纪录片 / 采访", "desc": "方言多、画面字多：画面字全扫，人名条 / 地名的译文贴在原文旁",
        "config": {"ocr": {"enabled": True, "fps": 3.0}, "asr": {"enabled": True},
                   "render": {"profiles": [_BILINGUAL, _SRT]}},
    },
    "anime": {
        "name": "动画", "desc": "台词以字幕和语音为主；画面字少扫一些，说明文字放顶部",
        "config": {"ocr": {"enabled": True, "fps": 2.0}, "asr": {"enabled": True, "separate": "on", "model": "large-v3-turbo"},
                   "render": {"profiles": [dict(_BILINGUAL, info_mode="top"), _SRT]}},
    },
    "variety": {
        "name": "综艺", "desc": "花字多、切换快：画面字扫得更密，说明文字放顶部",
        "config": {"ocr": {"enabled": True, "fps": 4.0}, "asr": {"enabled": True, "vad_threshold": 0.3},
                   "render": {"profiles": [dict(_BILINGUAL, info_mode="top"), _SRT]}},
    },
    "lecture": {
        "name": "讲座 / 播客", "desc": "只用语音（和自带字幕），不扫画面字；输出纯译文",
        "config": {"ocr": {"enabled": False}, "asr": {"enabled": True},
                   "render": {"profiles": [_SRT, _BILINGUAL]}},
    },
    "mv": {
        "name": "音乐 MV / 学语言", "desc": "原文全注音 + 整行罗马字 / 拼音 + 译文",
        "config": {"ocr": {"enabled": False}, "asr": {"enabled": True}, "render": {"profiles": [_LEARN, _BILINGUAL]}},
    },
    "custom": {"name": "自定义", "desc": "全用默认值", "config": {}},
}

USER = "user:"   # 自己的预设的 id 前缀
LEGACY_NAME = "我的默认"


# ---------------- 自己的预设 ----------------
def user_dir():
    return env.home() / "presets"


def _index_path():
    return user_dir() / "index.json"


def _index() -> dict:
    from .core.store import load_json

    return load_json(_index_path(), {"default": "", "desc": {}})


def _save_index(idx: dict):
    from .core.store import save_json

    user_dir().mkdir(parents=True, exist_ok=True)
    save_json(_index_path(), idx)


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or re.search(r'[\\/:*?"<>|]', name) or name.startswith("."):
        raise ValueError("预设名不能是空的，也不能有 \\ / : * ? \" < > |")
    return name


def user_file(name: str):
    return user_dir() / f"{name}.toml"


def user_names() -> list[str]:
    d = user_dir()
    return sorted(f.stem for f in d.glob("*.toml")) if d.is_dir() else []


def read_user(name: str) -> dict | None:
    from .core.config import read_toml, upgrade_file

    f = user_file(name)
    if not f.is_file():
        return None
    upgrade_file(f)
    return read_toml(f)


def save_user(name: str, raw: dict, desc: str | None = None):
    """写一个自己的预设（完整的一份设置：整套样式、格式版本；只属于一集的项去掉）。"""
    import tomli_w

    from .core import config

    name = check_name(name)
    raw = strip_episode(raw)
    cfg = config.Config.model_validate(config.merge_layers([raw]))
    raw = config.with_own_styles(raw, cfg)
    user_dir().mkdir(parents=True, exist_ok=True)
    f = user_file(name)
    if f.is_file():
        config.write_backup(f, "old", every=600)
    f.write_text(tomli_w.dumps(raw), encoding="utf-8")
    if desc is not None:
        idx = _index()
        idx.setdefault("desc", {})[name] = desc
        _save_index(idx)


def delete_user(name: str):
    from .core import config

    f = user_file(name)
    if f.is_file():
        config.write_backup(f, "deleted")
        f.unlink()
    idx = _index()
    idx.get("desc", {}).pop(name, None)
    if idx.get("default") == USER + name:
        idx["default"] = ""
    _save_index(idx)


def set_default(preset_id: str):
    idx = _index()
    idx["default"] = preset_id
    _save_index(idx)


def default_id() -> str:
    d = _index().get("default") or ""
    if d.startswith(USER) and d[len(USER):] not in user_names():
        return "documentary"
    return d or "documentary"


def default_user() -> tuple[str, dict | None]:
    """新项目默认用的自己的预设（内置预设不算：CLI 新项目照旧全用默认值）。"""
    d = _index().get("default") or ""
    if d.startswith(USER):
        name = d[len(USER):]
        return name, read_user(name)
    return "", None


def absorb_legacy(rest: dict):
    """旧的全局配置里的处理参数存进“我的默认”预设（已有就合并进去），没设默认预设时设成默认。"""
    from .core.config import deep_merge

    old = read_user(LEGACY_NAME) or {}
    old.pop("version", None)
    save_user(LEGACY_NAME, deep_merge(old, copy.deepcopy(rest)), "从以前的全局配置迁移过来")
    if not _index().get("default"):
        set_default(USER + LEGACY_NAME)


# 只属于一集、不进预设的项
EPISODE_ONLY = {"project": ("series", "context", "preset"), "subs": ("path", "track")}


def strip_episode(raw: dict, langs=True) -> dict:
    raw = copy.deepcopy(raw or {})
    for sec, keys in EPISODE_ONLY.items():
        part = raw.get(sec)
        if isinstance(part, dict):
            for k in keys:
                part.pop(k, None)
    if not langs and isinstance(raw.get("project"), dict):
        raw["project"].pop("source", None)
        raw["project"].pop("targets", None)
    if isinstance(raw.get("project"), dict) and not raw["project"]:
        raw.pop("project")
    return raw


# ---------------- 列表、取预设 ----------------
def listing():
    idx = _index()
    default = default_id()
    out = [{"id": k, "name": v["name"], "desc": v["desc"], "builtin": True, "default": k == default} for k, v in PRESETS.items()]
    out += [{"id": USER + n, "name": n, "desc": idx.get("desc", {}).get(n, ""), "builtin": False, "default": USER + n == default}
            for n in user_names()]
    return out


def get_raw(preset_id: str) -> dict | None:
    """预设的设置（内置的是预设本身写的那几项；自己的是完整的一份）。"""
    if preset_id.startswith(USER):
        return read_user(preset_id[len(USER):])
    p = PRESETS.get(preset_id)
    return copy.deepcopy(p["config"]) if p else None


def name_of(preset_id: str) -> str:
    if preset_id.startswith(USER):
        return preset_id[len(USER):]
    return PRESETS.get(preset_id, {}).get("name", preset_id)


def build(preset_id, source="auto", targets=None, series="", context=""):
    """生成 project.toml 的内容（dict）。作品已经有默认设置（series.toml）时，复制一份作品设置作为这一集的设置
    （填了节目说明就用填的），不用预设；之后这一集和作品各自独立。"""
    from .core.config import has_series_settings, read_series

    if series and has_series_settings(series):
        cfg = read_series(series)
        cfg.setdefault("project", {})["series"] = series
        if context:
            cfg["project"]["context"] = context
        return cfg
    cfg = get_raw(preset_id or "custom")
    if cfg is None:
        preset_id, cfg = "custom", {}
    project = dict(cfg.get("project") or {})
    if source or "source" not in project:
        project["source"] = source or "auto"
    if targets:
        project["targets"] = list(targets)
    project["preset"] = preset_id
    if series:
        project["series"] = series
    if context:
        project["context"] = context
    cfg["project"] = project
    return cfg

