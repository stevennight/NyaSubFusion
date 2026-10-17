"""内容预设：按节目类型打包的推荐配置，新建项目时写进 project.toml（之后都可以再改）。"""

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
        "config": {"ocr": {"enabled": True, "fps": 2.0}, "asr": {"enabled": True},
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
    "custom": {"name": "自定义", "desc": "只用全局 / 作品配置，不额外设置", "config": {}},
}


def listing():
    return [{"id": k, "name": v["name"], "desc": v["desc"]} for k, v in PRESETS.items()]


def build(preset_id, source="auto", targets=None, series="", context=""):
    """生成 project.toml 的内容（dict）。"""
    import copy

    cfg = copy.deepcopy(PRESETS.get(preset_id, PRESETS["custom"])["config"])
    project = {"source": source or "auto", "preset": preset_id}
    if targets:
        project["targets"] = list(targets)
    if series:
        project["series"] = series
    if context:
        project["context"] = context
    cfg["project"] = project
    return cfg
