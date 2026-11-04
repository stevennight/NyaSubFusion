"""分层配置：内置默认 < 用户全局 < 项目 < --config < 命令行参数。

样式（render.styles）：version >= 2 的配置里写的是一整套样式，后面的层整套替换前面的，不再按名字补回内置样式
（所以能删）；每个样式缺的字段按它来自的出厂样式（base，或同名的出厂样式）补齐。

用户全局配置（version 3 起）只放跟这台电脑有关的项（MACHINE_KEYS：接口、显卡、下载镜像），不再实时影响项目。
以前写在全局里的识别、输出参数第一次读的时候挪走：另存成“我的默认”预设（新项目默认用它），再留一份
legacy_global.toml；旧的项目 / 作品配置第一次读的时候升级——写上当时合并出来的整套样式、补上以前从全局
继承的项（结果不变），原文件备份到 backup/。还没有 project.toml 的新项目用默认预设。

作品（series）的设置不参与合并：它只是新建这部作品的一集时复制过去的默认值，之后两份各自独立
（可以用作品设置覆盖一集，或者用一集的设置覆盖作品，见文件末尾）。

所有层都是 TOML，按键深度合并（列表整体替换），最后用 pydantic 校验。
"""
import copy
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import env


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCfg(_M):
    source: str = "auto"                     # 源语言：ja / zh-Hans / en …；auto = 按语音或字幕自动判断
    targets: list[str] = ["zh-Hans"]         # 目标语言，可多个；第一个用于融合+翻译，其余在其基础上翻译
    series: str = ""                         # 所属作品：共享节目说明、术语表
    context: str = ""                        # 节目说明，会告诉大模型
    preset: str = ""                         # 新建项目时选的内容预设（不属于作品时，它就是这一集的模板）


class SubsCfg(_M):
    enabled: bool = True
    path: str = ""                           # 指定字幕文件；空 = 自动找同名字幕 / 内封字幕
    track: int | None = None                 # 指定第几条内封字幕轨（从 0 开始）
    ruby_size_ratio: float = 0.7             # 字号小于正文这个比例的行视为振假名小字，丢弃


class AsrCfg(_M):
    enabled: bool = True
    model: str = "large-v3"
    device: Literal["cuda", "cpu"] = "cuda"
    compute_type: str = "float16"
    beam_size: int = 5
    vad: bool = True
    vad_threshold: float = 0.25              # 默认 0.5 会漏掉小声说话（采访里很常见）
    separate: Literal["auto", "on", "off"] = "auto"  # 人声分离（去掉背景音乐再识别）：auto = 没有字幕时才做
    download_root: str = ""
    hf_endpoint: str = ""                    # 模型下载镜像，例如 https://hf-mirror.com
    backend: Literal["local", "api"] = "local"  # local：本机 faster-whisper；api：云端接口（按时长付费，见 sources/asr_api.py）
    api_kind: Literal["transcribe", "gemini"] = "transcribe"  # transcribe：/v1/audio/transcriptions；gemini：多模态大模型听音频
    api_provider: str = ""                   # 用哪个接口（llm.providers 里的 name）；空 = 和翻译用同一个
    api_model: str = "gpt-transcribe"        # 空 = 按 api_kind 的默认模型
    api_chunk_seconds: float = 0             # 每块上传多长（秒）；0 = 自动：OpenRouter 3 分钟（它上游 60 秒超时），其它 10 分钟


class Region(_M):
    rect: tuple[float, float, float, float]  # 相对画面比例 x0, y0, x1, y1
    exclude: bool = False                    # 排除区（台标、固定角标）
    name: str = ""


class OcrCfg(_M):
    enabled: bool = True
    fps: float = 3.0
    height: int = 720
    use_cuda: bool = True
    hwaccel: bool = True                     # 显卡解码，失败自动退回 CPU
    det_model: str = "auto"                  # auto：有 GPU 用 medium，只有 CPU 时用 small（快 4 倍）
    rec_model: str = "auto"
    min_score: float = 0.6
    min_duration: float = 0.6
    max_gap: float = 0.75
    persistent_seconds: float = 45.0
    regions: list[Region] = Field(default_factory=lambda: [Region(rect=(0.0, 0.0, 1.0, 1.0))])
    skip_static: bool = True
    keep_frames: bool = False
    refine: Literal["none", "vlm"] = "none"  # vlm：视觉大模型复核
    # 复核方式：frame = 整帧画面（模型自己读整帧，再和本地按位置对上，能合并切开的标题、补出漏掉的字）；
    # crop = 本地截图（本地截的特写连同认出的字几处一批问，省，但只能改错字、判断类型）
    refine_mode: Literal["frame", "crop"] = "frame"
    refine_frame_width: int = 1280           # 整帧画面方式：给模型读的整帧画面缩到多宽：1280 一张约 1300 token；1024 约 700，小字容易读错（小于 1024 按 1280）
    refine_context: bool = True              # 本地截图方式：附上框了编号的整帧画面（同一时刻的几处共用一张），判断是叠加字幕还是实物上的字准得多
    refine_context_width: int = 640          # 本地截图方式：附的整帧画面缩到多宽：640 一张约 300 token，看清字在什么东西上够了；1024 / 1280 更清楚，约 2 / 4 倍
    refine_boxed: bool = True                # 本地截图方式：用截图那一帧重新截特写、框出要转写的字（镜头移动时原来的特写会带进片名等别处的字）
    refine_batch: int = 4                    # 本地截图方式：几处一批问：越多越省；1 = 每处单独问，不会把别的图上的字写过来（串图）


class FusionCfg(_M):
    auto_sync: bool = True
    dup_similarity: float = 70
    asr_match_similarity: float = 60
    asr_uncovered_ratio: float = 0.3
    snap_seconds: float = 0.7


class ProviderCfg(_M):
    """一个 OpenAI 兼容接口。API Key 存在系统凭据库（按 name），配置里不写。"""
    name: str
    base_url: str
    model: str = ""
    vision_model: str = ""                   # 这个接口用于视觉复核时的模型；空 = 用 model
    api_key_env: str = ""                    # 凭据库里没有时，从这个环境变量取
    api_key: str = ""                        # 不推荐：明文写在配置里


class LlmCfg(_M):
    mode: Literal["auto", "api", "export", "off"] = "auto"  # auto：配了接口就调接口，否则导出任务文件
    providers: list[ProviderCfg] = []        # 接口列表（设置页管理，存在用户全局配置里）
    translate: str = ""                      # 翻译用哪个接口（providers 里的 name）；空 = 用下面的旧写法
    vision: str = ""                         # 视觉复核用哪个接口；空 = 和翻译用同一个
    asr: str = ""                            # 云端语音识别默认用哪个接口；空 = 和翻译用同一个（一集里可以用 asr.api_provider 单独换）
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    model: str = ""
    vision_model: str = ""
    temperature: float = 0.3
    chunk_seconds: float = 150
    chunk_max_items: int = 60
    frames: bool = True                      # 翻译时每条候选字幕附一张画面小图（看场景判断多义词，比如「揚げる」是炸还是收网）；接口不支持图片时自动不带
    # 分组按顺序翻（翻译一步）：整理好的字幕先整理一份人物表（同一个人 / 动物的各种叫法），再按话题分成几句一组，
    # 从头到尾一组一组重新翻（带前后文、前面的译法和这几句的画面）。界面上的“精翻”= 这个 + 思考程度 high
    grouped: bool = False
    # 翻译请求的思考程度（reasoning_effort，比如 low / medium / high，各模型支持的档不一样）；空 = 不设。
    # 分组翻时只用在分组翻那一轮；不分组时用在整理那一步（整理时顺带给出的译文就是最终译文）
    reasoning_effort: str = ""
    json_mode: bool = True
    timeout: float = 180
    max_retries: int = 3


class StyleCfg(_M):
    font: str = "Microsoft YaHei"
    size: float = 60
    color: str = "#FFFFFF"
    outline_color: str = "#101010"
    outline: float = 3
    shadow: float = 1
    bold: bool = False
    italic: bool = False
    spacing: float = 0                       # 字间距（1080p 画布上的像素）
    box: bool = False                        # 文字后面垫底色框（ASS 的 BorderStyle 3）；这时 outline 是框的留边，没有描边
    box_color: str = "#000000"
    box_opacity: float = 0.6                 # 底色框不透明度：1 = 不透明
    base: str = ""                           # 从哪个出厂样式来的（恢复出厂用）；空 = 同名的出厂样式


class AnnotateCfg(_M):
    """字上方的注音。能用哪种由这一层的语言决定：日语 furigana；中文 pinyin / zhuyin。"""
    kind: Literal["none", "furigana", "pinyin", "zhuyin"] = "none"
    scope: Literal["all", "difficult", "proper", "first"] = "all"  # 振假名范围：全部 / 难读 / 专有名词 / 每个词首次出现
    easy_kanji: Literal["grade1", "grade2"] = "grade2"  # difficult：只由这些小学低年级汉字组成的词不注
    known_words: list[str] = []              # difficult：已经会的词，不注
    numbers: bool = False                    # 数字（五十年、3人）也注音；读音容易错，默认不注
    script: Literal["hiragana", "katakana", "romaji"] = "hiragana"
    tone: Literal["mark", "number", "none"] = "mark"  # 拼音声调：pīn / pin1 / pin
    size: float = 0.5                        # 注音字号 = 底字 × size
    color: str = ""                          # 空 = 与底字同色
    srt: Literal["paren", "none"] = "paren"  # SRT 不支持注音：写成 漢字(かんじ)，或不输出


class LayerCfg(_M):
    role: Literal["original", "translation", "transliteration"]
    lang: str = ""                           # translation：目标语言（空 = 第一个目标语言）
    of: str = "original"                     # transliteration：转写哪一层（original 或某个目标语言代码）
    show: Literal["on", "off", "auto"] = "on"  # auto：画面上已有原文硬字幕时不显示（只对原文有意义）
    style: str = "main"
    max_chars: int = 0                       # 每行字数；0 = 用语言包默认值
    annotate: AnnotateCfg = AnnotateCfg()
    tone: Literal["mark", "number", "none"] = "mark"  # transliteration 为拼音时的声调写法


class ProfileCfg(_M):
    name: str
    enabled: bool = True
    format: Literal["ass", "srt"] = "ass"
    filename: str = "{stem}.{tr}.{ext}"      # 可用 {stem} {tr}（第一个译文层语言）{src} {ext} {name}
    layers: list[LayerCfg]
    info_mode: Literal["near", "top", "off"] = "near"  # 说明 / 人名条：贴原文旁 / 顶部 / 不显示
    info_style: str = "info"
    info_vertical: bool = True               # 原文是竖排的说明（人名条）：译文是中文 / 日文时也竖排
    avoid_hardsub: bool = True
    max_lines: int = 2
    min_duration: float = 1.0
    # 几乎同时开始的几条字幕（两个人同时说、旁白和画面上的台词同时出现）：
    # stack 各自成块、各自的时间，同时显示时上下错开；merge 合成一屏（每条的译文和原文仍挨在一起）
    simultaneous: Literal["stack", "merge"] = "stack"


DEFAULT_STYLES = {
    "main": StyleCfg(),
    "sub": StyleCfg(font="Yu Gothic", size=38, color="#E8E8E8", outline=2.4),
    "orig_main": StyleCfg(font="Yu Gothic", size=54),
    "info": StyleCfg(size=42, color="#FFE680", outline=2.4),
    "translit": StyleCfg(font="Segoe UI", size=30, color="#A8D8FF", outline=2),
}

DEFAULT_PROFILES = [
    ProfileCfg(name="bilingual", filename="{stem}.{tr}.{ext}", layers=[
        LayerCfg(role="translation", style="main"),
        LayerCfg(role="original", show="auto", style="sub"),
    ]),
    ProfileCfg(name="translation", format="srt", filename="{stem}.{tr}.{ext}", info_mode="top", layers=[
        LayerCfg(role="translation", style="main"),
    ]),
]


class VideoOutCfg(_M):
    """输出视频：封装 MKV（字幕轨 + 字体，不重新编码）或压制硬字幕 MP4。"""
    mode: Literal["off", "mux", "burn"] = "off"  # 运行完自动做：off 不输出视频 / mux 封装 MKV / burn 压制硬字幕
    # 封装哪些字幕：方案名，"broadcast" = 广播字幕原样；空 = 所有输出的字幕（广播字幕原样看下面的 broadcast）
    profiles: list[str] = []
    keep_subs: bool = True                   # 封装：保留原视频里的字幕轨
    fonts: Literal["subset", "full", "none"] = "subset"  # 封装时嵌入字体：只含用到的字 / 完整字体 / 不嵌入（压制总是用子集）
    encoder: Literal["auto", "h264_nvenc", "libx264", "hevc_nvenc", "libx265"] = "auto"  # auto：先试显卡，再用 CPU
    quality: int = Field(20, ge=0, le=51)    # 压制画质（CRF / CQ）：越小越清晰、文件越大，常用 18~23
    height: int = 0                          # 压制时缩放到这个高度；0 = 原尺寸
    broadcast: bool = True                   # profiles 为空时，封装要不要也放进“广播字幕原样”（字幕来自广播时才有）
    default: str = ""                        # 封装时哪条设成默认字幕轨：方案名 / "broadcast"；空或不在封装的字幕里 = 第一条
    burn: str = ""                           # 压制哪个字幕：空 = 第一个启用的 ASS 方案；方案名（ASS / SRT 都行）；"broadcast" = 广播字幕原样


def factory_style(name: str, base: str = "") -> StyleCfg | None:
    """出厂样式：先看 base，再看同名的。"""
    return DEFAULT_STYLES.get(base) or DEFAULT_STYLES.get(name)


def fill_style(name: str, st: dict) -> dict:
    """一个样式缺的字段按它的出厂样式补齐（不是出厂来的就用默认值）。"""
    st = st.model_dump() if isinstance(st, StyleCfg) else dict(st or {})
    if st.get("base") == name:  # 来自同名的出厂样式：和不写一样
        st["base"] = ""
    fac = factory_style(name, st.get("base", ""))
    return {**(fac.model_dump() if fac else {}), **st}


class RenderCfg(_M):
    styles: dict[str, StyleCfg] = Field(default_factory=lambda: copy.deepcopy(DEFAULT_STYLES))
    fallback_style: str = "main"             # 兜底样式：图层 / 说明文字用的样式不存在时用它

    @field_validator("styles", mode="before")
    @classmethod
    def _fill_styles(cls, v):
        """写了的每个样式补齐字段；一个都没写时用整套出厂样式。（按层合并时补回内置样式的事在 load 里做）"""
        if not v:
            return {k: s.model_dump() for k, s in DEFAULT_STYLES.items()}
        for name in v:
            if "," in name or not name.strip():
                raise ValueError(f"样式名「{name}」不能是空的，也不能有逗号（ASS 用逗号分隔）")
        return {name: fill_style(name, st) for name, st in v.items()}

    def style(self, name: str) -> StyleCfg:
        """按名字取样式；没有就用兜底样式，兜底也没有就用第一个。"""
        return self.styles.get(name) or self.styles.get(self.fallback_style) or next(iter(self.styles.values()))

    profiles: list[ProfileCfg] = Field(default_factory=lambda: copy.deepcopy(DEFAULT_PROFILES))
    video: VideoOutCfg = VideoOutCfg()
    merged_debug: bool = True                # 另外输出按来源上色的原文合并字幕，方便检查
    # 字幕来自广播（ARIB）时，另外输出一份原样的原文 ASS：位置、字号、颜色、背景、振假名照搬（视频名.ja-broadcast.ass）
    broadcast: bool = True
    broadcast_font: str = ""                 # 广播字幕原样用的字体；空 = 自动（圆体优先，其次等宽的日文字体）
    # 输出前从原文、译文里去掉的符号。默认去掉日文字幕行尾的“➡”（表示这句话接到下一条字幕）
    strip_marks: list[str] = Field(default_factory=lambda: ["➡"])
    lang_options: dict[str, dict] = Field(default_factory=dict)  # 按语言的排版选项，见各语言包


VERSION = 3   # 配置格式：2 = 样式是整套的；3 = 不再从全局配置继承识别、输出参数（见文件开头）

# 用户全局配置里只留这些（跟这台电脑 / 这个人有关的），其余是每一集自己的处理参数
MACHINE_KEYS = {
    "llm": {"providers", "translate", "vision", "asr", "base_url", "api_key", "api_key_env", "model", "vision_model",
            "timeout", "max_retries", "json_mode"},
    "asr": {"device", "compute_type", "download_root", "hf_endpoint"},
    "ocr": {"use_cuda", "hwaccel"},
}


class Config(_M):
    version: int = 1
    project: ProjectCfg = ProjectCfg()
    subs: SubsCfg = SubsCfg()
    asr: AsrCfg = AsrCfg()
    ocr: OcrCfg = OcrCfg()
    fusion: FusionCfg = FusionCfg()
    llm: LlmCfg = LlmCfg()
    render: RenderCfg = RenderCfg()

    def pick(self, *keys) -> dict:
        """取出若干配置项（"asr.model" 或整段 "ocr"），用于计算步骤指纹。"""
        data = self.model_dump(mode="json")
        out = {}
        for k in keys:
            cur = data
            for part in k.split("."):
                cur = cur[part]
            out[k] = cur
        return out

    def endpoint(self, purpose="translate") -> dict:
        """某个用途实际用的接口：{"name", "base_url", "model", "key"}。key 只在内存里，不落盘。"""
        from .. import secrets

        c = self.llm
        name = (c.vision or c.translate) if purpose == "vision" else c.translate
        if purpose == "asr":  # 云端语音识别：接口凭据和翻译共用一套，模型单独设
            name = self.asr.api_provider or c.asr or c.translate
        p = next((x for x in c.providers if x.name == name), None) if name else None
        if p:
            model = (p.vision_model or p.model) if purpose == "vision" else p.model
            if purpose == "asr":
                model = self.asr_api_model()
            key = secrets.get(p.name) or (os.environ.get(p.api_key_env, "") if p.api_key_env else "") or p.api_key
            return {"name": p.name, "base_url": p.base_url, "model": model, "key": key}
        model = (c.vision_model or c.model) if purpose == "vision" else c.model
        if purpose == "asr":
            model = self.asr_api_model()
        key = c.api_key or (os.environ.get(c.api_key_env, "") if c.api_key_env else "")
        return {"name": "", "base_url": c.base_url, "model": model, "key": key}

    def asr_api_model(self) -> str:
        from ..sources.asr_api import DEFAULT_MODEL

        return self.asr.api_model or DEFAULT_MODEL[self.asr.api_kind]

    def llm_mode(self) -> str:
        if self.llm.mode != "auto":
            return self.llm.mode
        ep = self.endpoint()
        return "api" if (ep["key"] and ep["model"]) else "export"

    def api_key(self) -> str:
        return self.endpoint()["key"]


def deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def read_toml(path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def series_dir(name: str) -> Path:
    return env.home() / "series" / name


def owns_styles(d: dict) -> bool:
    """这一层写的是一整套样式（新格式）。"""
    return (d.get("version") or 1) >= 2 and isinstance((d.get("render") or {}).get("styles"), dict)


def merge_layers(layer_dicts: list[dict]) -> dict:
    """按层深度合并。整套样式的层把前面的样式整套换掉；没有哪一层是整套样式时，按名字合并到出厂样式上（旧行为）。"""
    merged: dict = {}
    if not any(owns_styles(d) for d in layer_dicts):
        merged = {"render": {"styles": {k: s.model_dump() for k, s in DEFAULT_STYLES.items()}}}
    for d in layer_dicts:
        if owns_styles(d):
            merged.setdefault("render", {})["styles"] = {}
        deep_merge(merged, d)
    return merged


def split_machine(raw: dict) -> tuple[dict, dict]:
    """配置拆成 (跟这台电脑有关的, 处理参数)。"""
    machine, rest = {}, copy.deepcopy(raw or {})
    for sec, keys in MACHINE_KEYS.items():
        part = rest.get(sec)
        if not isinstance(part, dict):
            continue
        for k in keys & set(part):
            machine.setdefault(sec, {})[k] = part.pop(k)
        if not part:
            rest.pop(sec)
    rest.pop("version", None)
    return machine, rest


def fill_missing(dst: dict, src: dict) -> dict:
    """src 里有、dst 里没写的项补进 dst（dst 写了的不动）。"""
    for k, v in src.items():
        if k not in dst:
            dst[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            fill_missing(dst[k], v)
    return dst


def _user_file() -> Path:
    return env.home() / "config.toml"


def legacy_file() -> Path:
    return env.home() / "legacy_global.toml"


def write_backup(path: Path, tag: str, every: float = 0):
    """改之前留一份到旁边的 backup/。every>0（自动保存用）：离上一份同类备份不到这么多秒就不再存，
    连着改很多下时只留改之前的那一份，不刷出一堆。"""
    import time

    path = Path(path)
    if every:
        olds = (path.parent / "backup").glob(f"{path.stem}-*-{tag}{path.suffix}")
        if time.time() - max((p.stat().st_mtime for p in olds), default=0) < every:
            return
    bak = path.parent / "backup" / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}-{tag}{path.suffix}"
    bak.parent.mkdir(parents=True, exist_ok=True)
    bak.write_bytes(path.read_bytes())


def migrate_global() -> bool:
    """全局配置里的处理参数挪走：留一份 legacy_global.toml（给还没升级的旧项目补用），另存成“我的默认”预设。"""
    import tomli_w

    user = _user_file()
    if not user.is_file():
        return False
    raw = read_toml(user)
    machine, rest = split_machine(raw)
    if not rest:
        return False
    lf = legacy_file()
    legacy = read_toml(lf) if lf.is_file() else {}
    lf.write_text(tomli_w.dumps(deep_merge(legacy, copy.deepcopy(rest))), encoding="utf-8")
    from .. import presets

    presets.absorb_legacy(rest)
    write_backup(user, "v2")
    user.write_text(tomli_w.dumps(machine), encoding="utf-8")
    return True


def legacy_global() -> dict:
    """以前的全局配置（挪到 legacy_global.toml 的 + 现在的）。"""
    out = read_toml(legacy_file()) if legacy_file().is_file() else {}
    return deep_merge(out, read_toml(_user_file()) if _user_file().is_file() else {})


def load(config_path=None, project_file=None, overrides: dict | None = None, upgrade=True) -> tuple[Config, list[str]]:
    """按层合并配置，返回 (配置, 实际用到的文件列表)。旧格式的项目配置先升级；还没有项目配置时用默认预设。"""
    migrate_global()
    if project_file and upgrade:
        upgrade_file(project_file)
    layers: list[tuple[str, dict]] = []
    user = _user_file()
    if user.is_file():
        layers.append((str(user), split_machine(read_toml(user))[0]))
    tail = []
    if project_file and Path(project_file).is_file():
        tail.append((str(project_file), read_toml(project_file)))
    elif project_file:
        from .. import presets

        name, raw = presets.default_user()
        if raw:
            tail.append((f"预设「{name}」", raw))
    if config_path:
        tail.append((str(config_path), read_toml(config_path)))
    if overrides:
        tail.append(("命令行", overrides))

    merged = merge_layers([d for _, d in layers + tail])
    return Config.model_validate(merged), [name for name, _ in layers + tail]


def with_own_styles(raw: dict, cfg: "Config") -> dict:
    """配置里写上整套样式（用 cfg 合并出来的样式）和格式版本。"""
    raw = copy.deepcopy(raw or {})
    if not owns_styles(raw):
        styles = {}
        for name, st in cfg.render.styles.items():
            d = st.model_dump()
            if not d.get("base") and name in DEFAULT_STYLES:
                d.pop("base", None)
            styles[name] = d
        raw.setdefault("render", {})["styles"] = styles
    raw["version"] = max(VERSION, raw.get("version") or 1)
    return raw


def upgrade_file(path) -> bool:
    """旧格式的项目 / 作品配置升级（结果不变）：
    version < 2：写上当时合并出来的整套样式；version < 3：以前从全局配置继承的处理参数补进来。原文件先备份到 backup/。"""
    import tomli_w

    path = Path(path)
    if not path.is_file():
        return False
    raw = read_toml(path)
    ver = raw.get("version") or 1
    if ver >= VERSION:
        return False
    old_global = legacy_global()
    new = copy.deepcopy(raw)
    if ver < 2:
        new.pop("version", None)
        new = with_own_styles(new, Config.model_validate(merge_layers([old_global, new])))
    rest = split_machine(old_global)[1]
    (rest.get("render") or {}).pop("styles", None)
    fill_missing(new, rest)
    new["version"] = VERSION
    write_backup(path, f"v{ver}")
    path.write_text(tomli_w.dumps(new), encoding="utf-8")
    return True


# ---------------- 作品（series）的默认设置 ----------------
# 新建这部作品的一集时复制一份过去；之后一集和作品各自独立。
# 只属于这一集、复制 / 覆盖时都不动的项：所属作品、外挂字幕文件
EPISODE_KEYS = {"project": ("series",), "subs": ("path", "track")}


def series_file(name: str) -> Path:
    return series_dir(name) / "series.toml"


def has_series_settings(name: str) -> bool:
    return bool(name) and series_file(name).is_file()


def read_series(name: str) -> dict:
    f = series_file(name)
    if not (name and f.is_file()):
        return {}
    upgrade_file(f)
    return read_toml(f)


def series_effective(name: str) -> "Config":
    """作品的默认设置合并后是什么样（内置默认 < 用户全局 < 作品设置）。"""
    f = series_file(name)
    if name and f.is_file():
        upgrade_file(f)
    return load(str(f) if f.is_file() else None)[0]


def split_episode(raw: dict) -> tuple[dict, dict]:
    """项目配置拆成 (只属于这一集的, 可以放进作品的)。"""
    raw = copy.deepcopy(raw or {})
    mine: dict = {}
    for sec, keys in EPISODE_KEYS.items():
        part = raw.get(sec)
        if not isinstance(part, dict):
            continue
        for k in keys:
            if k in part:
                mine.setdefault(sec, {})[k] = part.pop(k)
        if not part:
            raw.pop(sec)
    return mine, raw


def episode_from_series(project_raw: dict, series_raw: dict) -> dict:
    """用作品设置覆盖一集：作品设置复制一份，这一集的所属作品、外挂字幕文件留着。"""
    mine, _ = split_episode(project_raw)
    out = copy.deepcopy(series_raw or {})
    for sec, part in mine.items():
        out.setdefault(sec, {}).update(part)
    return out


def series_from_episode(project_raw: dict) -> dict:
    """用一集的设置覆盖作品：这一集的设置去掉只属于它的项。"""
    return split_episode(project_raw)[1]


def diff_settings(a: dict, b: dict, prefix="", keys=()) -> list[dict]:
    """两份（合并后的）设置哪些项不一样：[{"path", "keys", "before", "after"}]。列表整体比较；keys 是逐级的键（样式名里可能有点）。"""
    out = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        path, ks = f"{prefix}{k}", [*keys, k]
        if isinstance(x, dict) and isinstance(y, dict):
            out += diff_settings(x, y, path + ".", ks)
        elif x != y:
            out.append({"path": path, "keys": ks, "before": x, "after": y})
    return out


def get_keys(d: dict, keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def set_keys(d: dict, keys, value) -> dict:
    """按逐级的键写进去；value 是 None 时删掉这一项。"""
    cur = d
    for k in keys[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    if value is None:
        cur.pop(keys[-1], None)
    else:
        cur[keys[-1]] = copy.deepcopy(value)
    return d


# 只属于一集、和模板比较时不算的项
TEMPLATE_SKIP = {("project", "series"), ("project", "preset"), ("project", "context"), ("subs", "path"), ("subs", "track"), ("version",)}


def template_diff(episode: "Config", template: "Config", langs=True) -> list[dict]:
    """一集和它的模板哪些项不一样（before = 这一集，after = 模板）。langs=False 时语言不算（内置预设不管语言）。"""
    skip = set(TEMPLATE_SKIP) | (set() if langs else {("project", "source"), ("project", "targets")})
    return [c for c in diff_settings(episode.model_dump(mode="json"), template.model_dump(mode="json"))
            if not any(tuple(c["keys"][:len(k)]) == k for k in skip)]
