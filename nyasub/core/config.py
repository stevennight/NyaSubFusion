"""分层配置：内置默认 < 用户全局 < 作品（series） < 项目 < --config < 命令行参数。

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
    preset: str = ""                         # 新建项目时选的内容预设（只做记录）


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
    download_root: str = ""
    hf_endpoint: str = ""                    # 模型下载镜像，例如 https://hf-mirror.com


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


class FusionCfg(_M):
    auto_sync: bool = True
    dup_similarity: float = 70
    asr_match_similarity: float = 60
    asr_uncovered_ratio: float = 0.3
    snap_seconds: float = 0.7


class LlmCfg(_M):
    mode: Literal["auto", "api", "export", "off"] = "auto"  # auto：配了接口就调接口，否则导出任务文件
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    model: str = ""
    vision_model: str = ""
    temperature: float = 0.3
    chunk_seconds: float = 150
    chunk_max_items: int = 60
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
    avoid_hardsub: bool = True
    max_lines: int = 2
    min_duration: float = 1.0


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


class RenderCfg(_M):
    styles: dict[str, StyleCfg] = Field(default_factory=lambda: copy.deepcopy(DEFAULT_STYLES))

    @field_validator("styles", mode="before")
    @classmethod
    def _merge_styles(cls, v):
        """用户写的样式按名字合并到内置样式上：只写 size 也只改字号，其他内置样式保留。"""
        out = {k: s.model_dump() for k, s in DEFAULT_STYLES.items()}
        for name, st in (v or {}).items():
            st = st.model_dump() if isinstance(st, StyleCfg) else dict(st)
            out[name] = {**out.get(name, {}), **st}
        return out

    profiles: list[ProfileCfg] = Field(default_factory=lambda: copy.deepcopy(DEFAULT_PROFILES))
    merged_debug: bool = True                # 另外输出按来源上色的原文合并字幕，方便检查
    lang_options: dict[str, dict] = Field(default_factory=dict)  # 按语言的排版选项，见各语言包


class Config(_M):
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

    def llm_mode(self) -> str:
        if self.llm.mode != "auto":
            return self.llm.mode
        return "api" if (self.api_key() and self.llm.model) else "export"

    def api_key(self) -> str:
        return self.llm.api_key or (os.environ.get(self.llm.api_key_env, "") if self.llm.api_key_env else "")


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


def load(config_path=None, project_file=None, overrides: dict | None = None) -> tuple[Config, list[str]]:
    """按层合并配置，返回 (配置, 实际用到的文件列表)。"""
    layers: list[tuple[str, dict]] = []
    user = env.home() / "config.toml"
    if user.is_file():
        layers.append((str(user), read_toml(user)))
    tail = []
    if project_file and Path(project_file).is_file():
        tail.append((str(project_file), read_toml(project_file)))
    if config_path:
        tail.append((str(config_path), read_toml(config_path)))
    if overrides:
        tail.append(("命令行", overrides))

    merged: dict = {}
    for _, d in layers + tail:
        deep_merge(merged, d)
    series = (merged.get("project") or {}).get("series")
    if series:
        sf = series_dir(series) / "series.toml"
        if sf.is_file():
            # 作品层在用户层之后、项目层之前
            layers.append((str(sf), read_toml(sf)))
            merged = {}
            for _, d in layers + tail:
                deep_merge(merged, d)
    return Config.model_validate(merged), [name for name, _ in layers + tail]
