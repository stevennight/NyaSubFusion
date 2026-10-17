# NyaSubFusion 技术设计

> 版本 v0.4 · 2026-10-17　·　v0.4：M3 服务与界面、M4 桌面打包已实现，第 10 节按实现更新
> 版本 v0.3 · 2026-10-17　·　v0.3：M1 内核、M2 注音已实现，6.2~6.4、7.2 按实现更新
> 版本 v0.2（草案）· 2026-10-16　·　v0.2：桌面壳定为 Tauri 2；新增打包分发；图层按“角色 × 语言”设计
> 配套：[PRD.md](PRD.md) · [交互原型](../prototype/index.html)

---

## 1. 总体架构

```
┌──────────── 桌面壳 Tauri 2（WebView2）· 开发时直接用浏览器 ────────────────┐
│ 原生能力：窗口 · 文件对话框 · 拖入文件 · 托盘/通知 · 自动更新 · 启动内核进程 │
│ ┌──────────────────────────── 前端 ────────────────────────────────────┐ │
│ │ 项目 · 流程 · 审校编辑器 · 图层样式 · 术语 · 导出 · 设置                │ │
│ │ React + TypeScript + Vite；时间轴 Canvas；字幕预览：HTML 图层 + JASSUB │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
└───────────────▲──────────────────────────────────────────▲────────────────┘
                │ REST（项目/字幕/配置）                    │ WebSocket（进度/日志）
┌───────────────┴──────────────────────────────────────────┴────────────────┐
│ 服务层 FastAPI：项目管理、任务队列、文件服务（代理视频、截图、波形）           │
├───────────────────────────────────────────────────────────────────────────┤
│ 内核 nyasub.core：数据模型 · 配置 · 流水线（步骤图 + 指纹缓存）               │
│                                                                           │
│  sources            fusion          llm              lang         render  │
│  ├ subtitle 读取    ├ align 对时     ├ providers      ├ ja         ├ layout │
│  ├ asr/*  插件      ├ dedupe 去重    ├ glossary       ├ zh         ├ ass    │
│  └ ocr/*  插件      ├ classify 分类  ├ fuse+translate ├ ko / en    ├ srt/vtt│
│                     └ timing 精修    └ qa             └ (插件)     └ burn   │
├───────────────────────────────────────────────────────────────────────────┤
│ 外部依赖：ffmpeg · faster-whisper · RapidOCR/PaddleOCR · OpenAI 兼容接口     │
└───────────────────────────────────────────────────────────────────────────┘
          ▲ 同一内核也提供 CLI：nyasub run 视频.ts --profile 双语 --until merge
```

### 技术选型

| 部分 | 选型 | 理由 |
|---|---|---|
| 内核 | Python 3.11+ | 识别生态（whisper、OCR、OpenCV）都在 Python；可直接复用 subfusion 代码 |
| 配置 / 数据模型 | pydantic v2 | 同一份模型生成校验、默认值、JSON Schema（前端表单直接用） |
| 服务 | FastAPI + uvicorn | 异步、WebSocket、自动 OpenAPI |
| 前端 | React + TS + Vite，状态 Zustand | 编辑器类交互多，生态成熟 |
| 时间轴 | 自绘 Canvas（波形用预计算峰值） | 长视频上千条字幕，DOM 方案太慢 |
| 字幕预览 | 编辑时用 HTML 图层渲染（`<ruby>` 原生支持）；“精确预览”用 JASSUB（libass 的 wasm 版）渲染导出的 ASS | 编辑时快、所见即所得；导出前可确认实际效果 |
| 桌面壳 | Tauri 2 | Windows 上用系统 WebView2（Edge 内核），界面能力与 Chrome 相同；自带自动更新、托盘、通知、文件关联、安装包生成；社区活跃 |
| 内核打包 | uv 管理依赖；PyInstaller（onedir）打成 `nyasub-core`，作为 Tauri 的 sidecar 后台程序 | 见第 10 节 |

**解耦原则**：前端只通过 HTTP / WebSocket 与 FastAPI 通信，不依赖 Tauri 的 JS 接口做业务；只有“选文件 / 选目录 / 打开所在文件夹 / 通知 / 更新”这类原生能力走 Tauri，并封装在 `web/src/platform/` 一层里（浏览器下有降级实现）。这样：

- M1~M3 开发阶段完全在浏览器里跑：`uv run nyasub serve` + `pnpm dev`，用 Chrome 开发者工具调试；
- 内核也能单独部署在有显卡的机器上，用浏览器远程访问；
- 桌面壳只在 M4 打包时接入，以后要换壳也只动这一层。

## 2. 目录结构

```
nyasub/
  core/
    model.py         # Project / Candidate / Cue / Token / Layer / Profile
    config.py        # 分层配置：默认 < 用户 < 作品 < 项目 < 单条
    pipeline.py      # 步骤图、指纹缓存、断点续跑
    store.py         # 项目目录读写（原子写）
  sources/
    subtitle.py      # srt/ass/vtt/ttml/ARIB，编码识别，振假名小字剔除
    imagesub.py      # PGS/VobSub/DVB → 图片 → OCR
    asr/             # base.py + faster_whisper.py + openai_api.py ...
    ocr/             # base.py + rapidocr.py + paddle.py + vlm_refine.py + tracker.py
  fusion/
    align.py         # 整体偏移 + 分段漂移
    dedupe.py classify.py fuse.py timing.py
  llm/
    providers.py     # OpenAI 兼容客户端、重试、JSON 模式、并发
    prompts/         # 按任务/语言对的模板（Jinja2），不再硬编码节目信息
    glossary.py translate.py qa.py export_jobs.py
  lang/
    base.py          # LanguagePack 接口
    ja.py zh.py ko.py en.py
  render/
    layout.py        # 断行、拆条、避让、注音排布（与格式无关的绝对坐标）
    fonts.py         # 字体度量（fontTools / uharfbuzz）
    ass.py srt.py vtt.py ttml.py burn.py
  server/            # FastAPI 路由、任务队列、WebSocket
  cli.py             # nyasub run / serve / doctor（环境检查）
web/                 # 前端工程（React + TS + Vite）
  src/platform/      # 原生能力封装：tauri.ts / browser.ts
desktop/             # Tauri 2 工程（src-tauri：sidecar 管理、更新、托盘）
packaging/           # PyInstaller spec、GPU 组件清单、安装包脚本
plugins/             # 第三方插件示例
```

## 3. 数据模型

### 3.1 项目目录

```
视频名.nyasub/
  project.json        # 项目元数据、语言对、所属作品、配置覆盖
  media/              # meta.json、audio.wav、proxy.mp4、peaks.json、shots.json（镜头切换点）
  stages/             # 每步结果 + 指纹：subs.json asr.json ocr.json fused.json translated.<lang>.json
  crops/              # 画面字截图
  llm_cache/          # 大模型分段结果（按请求内容哈希）
  edits.json          # 用户手动修改（独立保存，重跑时合并回去，不会被覆盖）
```

作品目录（跨集共享）：`<用户数据>/series/<作品名>/series.toml`（节目说明、术语表、风格、用户读音词典、样式）。

### 3.2 候选条目 Candidate

```jsonc
{
  "id": "o17",                 // c=字幕 o=画面字 a=语音
  "src": "ocr",                // subtitle | ocr | asr
  "start": 63.20, "end": 66.85,
  "text": "わしゃあ もう五十年\n海に出とる",
  "conf": 0.91,
  // 画面字特有
  "rect": [0.31, 0.80, 0.69, 0.93], "zone": "bottom", "vertical": false, "crop": "crops/o17.jpg",
  // 语音特有
  "words": [{"w": "わしゃ", "s": 63.4, "e": 63.8}],
  // 规则阶段写入
  "dropped": null,             // logo | junk | scene | dup
  "guess": "dialogue"          // 规则对类型的猜测，供大模型参考
}
```

### 3.3 字幕条 Cue（融合后的核心数据）

```jsonc
{
  "id": "q42",
  "start": 63.20, "end": 66.85,
  "type": "dialogue",          // dialogue narration info name title song sfx
  "speaker": "佐藤",
  "sources": ["c40", "o17", "a51"],   // 追溯
  "onscreen": true,            // 画面上已有原文硬字幕 → 原文图层 auto 时不显示
  "rect": null,                // info 类：原文在画面上的位置，用来贴放译文
  "orig": {
    "lang": "ja",
    "text": "わしゃあ もう五十年 海に出とる",
    "tokens": [                // 语言包分词 + 注音（见第 6 节）
      {"t": "わしゃあ"},
      {"t": "五十年", "r": "ごじゅうねん", "num": true},
      {"t": "海", "r": "うみ", "lv": 5},
      {"t": "に"}, {"t": "出", "r": "で", "lv": 5}, {"t": "とる"}
    ]
  },
  "tr": {                      // 按目标语言存，一个项目可以有多个目标语言
    "zh-Hans": {"text": "我出海已经五十年了", "alts": ["……"], "tokens": [/* 拼音由 zh 语言包生成 */]},
    "en":      {"text": "I've been going out to sea for fifty years."}
  },
  "flags": ["ocr_low_conf"],   // 审校队列依据
  "review": "todo",            // todo | ok
  "note": ""                   // 译注
}
```

### 3.4 图层与输出方案 Profile

**图层 = 角色 × 语言。** 角色决定这一层显示什么，语言决定这一层能用哪些注音和断行规则。注音不跟“原文 / 译文”绑定，而是跟这一层文字的语言走：英语视频翻成日语时，日语**译文**层一样可以加振假名。

| 角色 `role` | 内容 | 语言 `lang` |
|---|---|---|
| `original` | 融合后的原文 | 自动等于项目源语言 |
| `translation` | 某个目标语言的译文；一个方案里可以放多个（如中 + 英） | 项目目标语言之一 |
| `transliteration` | 某一层的整行转写（罗马字、整行拼音…） | 由 `of` 指定转写哪一层 |
| `info` | 说明 / 人名条 / 地名的译文，贴在原画面字旁 | 同第一个可见的译文层 |
| `note` | 译注（P2） | 任意 |

`annotate.kind` 只能取该语言包提供的选项（见 6.1），界面上的注音菜单也由语言包决定；切换语言时如果原注音不再支持，自动改为 `none` 并提示。

```toml
[project]
source  = "ja"
targets = ["zh-Hans", "en"]

[[profile]]
name = "中日双语"
format = "ass"
filename = "{stem}.zh-ja.ass"

  [[profile.layer]]                 # 按画面中从上到下的顺序
  role = "translation"
  lang = "zh-Hans"
  style = "主字幕"

  [[profile.layer]]
  role = "original"
  show = "auto"                     # on | off | auto（画面已有原文硬字幕时不显示）
  style = "副字幕"
  annotate = { kind = "furigana", scope = "difficult", level = "N2", script = "hiragana", size = 0.5 }

  [profile.info]                    # 说明类画面字
  mode = "near_source"              # near_source（贴原文旁）| top | off
  style = "说明"

  [profile.rules]
  avoid_hardsub = true
  max_lines = 2

[[profile]]
name = "中英双语"
format = "ass"
filename = "{stem}.zh-en.ass"
  [[profile.layer]]
  role = "translation"
  lang = "zh-Hans"
  annotate = { kind = "pinyin", tone = "mark" }
  [[profile.layer]]
  role = "translation"
  lang = "en"
  style = "英文小字"
  [[profile.layer]]
  role = "original"
  show = "off"

[[profile]]
name = "日语学习"
format = "ass"
filename = "{stem}.ja-ruby.ass"
  [[profile.layer]]
  role = "original"
  annotate = { kind = "furigana", scope = "all", script = "hiragana" }
  [[profile.layer]]
  role = "transliteration"
  of = "original"                   # 整行罗马字
  style = "小字"
  [[profile.layer]]
  role = "translation"
  lang = "zh-Hans"
  style = "副字幕"
```

样式（`style`）是独立的命名对象（字体、字号、颜色、描边、阴影、对齐、边距），作品和项目都能覆盖。

## 4. 流水线与缓存

### 4.1 步骤图

```
probe ──► extract_audio ──► asr ─────────────┐
   │                                          │
   ├──► proxy / peaks / shots                 ▼
   ├──► subtitle ─────────► align ─────► fuse_rules ──► llm_fuse_translate ──► annotate ──► render(×profile)
   └──► ocr_scan ──► ocr_refine(可选) ─────────┘              ▲                    ▲
                                                 glossary ────┘   user edits ──────┘
```

### 4.2 指纹缓存（替代前身的 dirty 标记）

每个步骤声明：

```python
class Stage:
    name = "ocr_scan"
    inputs = ["media.video"]                      # 上游产物
    config_keys = ["ocr.engine", "ocr.fps", "ocr.regions", "ocr.min_score"]  # 只有这些配置影响结果
    outputs = ["stages/ocr.json", "crops/"]
    version = 3                                   # 算法改了就 +1
```

指纹 = hash(上游产物指纹 + 相关配置 + 版本)。运行时从下游往上找第一个指纹不匹配的步骤开始重做。界面在用户改配置时就能算出“下列步骤将重做”，避免误操作。

- 字体、颜色等**样式改动只影响 render**，秒级出结果。
- 注音范围的改动只影响 annotate + render。
- 用户手动编辑保存在 `edits.json`（按 cue id + 字段），上游重跑后按时间重叠 + 文本相似度重新挂回去；挂不上的列出来让用户处理。

### 4.3 大模型调用

- 分段：按时长（默认 150 秒）和条数切块，块间带上一块末尾 3 条作为只读上下文。
- 每段请求按内容哈希缓存在 `llm_cache/`，失败只重试该段；并发数可配。
- 输出校验：每个输入 id 必须出现在某条 cue 的 `sources` 或 `drop` 中（沿用前身规则），不满足则自动重试一次，再不行回退为“原样保留 + 标记 needs_review”。

## 5. 融合算法（沿用并通用化）

前身 `merge.py` 的规则基本保留，改动点：

| 项 | 前身 | 新设计 |
|---|---|---|
| 时间对齐 | 整体偏移（逐词时间 + 模糊匹配，粗搜 0.5s 再细搜 0.1s） | 增加分段漂移：每 5 分钟窗口单独估计，再平滑成分段线性映射 |
| 来源可信度 | 写死：字幕 > 画面字 > 语音 | 权重可配；无字幕时自动提升画面字权重 |
| 画面字分类 | 底部居中 = 台词、靠左单行 = 人名条…（日语规则） | 规则下沉到语言包 + 预设（如动画预设里顶部字多为标题/歌词） |
| 幻觉过滤 | 日语固定列表 | 语言包提供列表 + 用户可加 |
| 结果 | items + 统计 | 另加 per-cue `flags`，供审校筛选 |
| 时间精修 | 起点吸附到语音开始 | + 镜头切换吸附、最短时长、阅读速度（CPS）检查、间隙闭合 |

问题标记（`flags`）初版：

| flag | 条件 |
|---|---|
| `ocr_low_conf` | OCR 置信度 < 0.85 |
| `asr_only` | 只有语音来源 |
| `conflict` | 同时段字幕/画面字与语音文本相似度低 |
| `llm_fallback` | 大模型结果校验失败后回退 |
| `cps_high` | 每秒字数超过语言包设定（中文默认 9，日文 8，英文 17） |
| `reading_guess` | 注音来自分词器且是专有名词 / 多音字 |

## 6. 语言包与注音

### 6.1 接口

每种语言一个语言包，负责这门语言的分词、注音、转写、断行和标点。原文和任何目标语言的译文都走同一套语言包，所以“支持一门新语言”就是加一个语言包插件。

```python
class LanguagePack(Protocol):
    code: str                                     # "ja" / "zh-Hans" / "en" ...
    annotations: list[type[AnnotationKind]]       # 逐字/逐词注音，显示在字上方
    transliterations: list[type[TranslitKind]]    # 整行转写
    def normalize(self, text: str) -> str: ...    # 全半角、空白、标点规范
    def tokenize(self, text: str) -> list[Token]: ...
    def annotate(self, tokens, kind: AnnotationKind) -> list[Token]: ...
    def transliterate(self, tokens, kind: TranslitKind) -> str: ...
    def line_break(self, text: str, max_chars: int) -> list[str]: ...   # 含禁则
    def punctuation(self, text: str, rules) -> str: ...
    direction: Literal["ltr", "rtl"]
    cps_default: float
    asr_hallucinations: list[str]
    ocr_model: str

class AnnotationKind(BaseModel):                  # 每种注音自带 pydantic 选项模型
    id: ClassVar[str]                             # "furigana"
    label: ClassVar[str]                          # "振假名"
```

`GET /api/langs` 把每个语言包的注音 / 转写选项及其 JSON Schema 发给前端，图层样式页据此生成表单，不用为每门语言单独写界面。

| 语言 | 注音（字上方） | 整行转写 | 分词 / 依赖 | 断行与排版 |
|---|---|---|---|---|
| ja 日语 | 振假名（平假名 / 片假名 / 罗马字；范围：全部 / 难读 / 专名 / 首次） | 罗马字（Hepburn） | SudachiPy（模式 C）、cutlet | 禁则；句末「。」去除可选 |
| zh-Hans / zh-Hant 中文 | 拼音（调号 / 数字 / 无调）、注音符号 | 整行拼音 | jieba、pypinyin（词组消歧）、OpenCC 简繁 | 句末去标点、句中逗号换空格（可选） |
| yue 粤语 | 粤拼 | 整行粤拼 | pycantonese | 同中文 |
| ko 韩语 | 汉字词标汉字（P2） | 罗马字（国语罗马字） | Kiwi | 按词断行 |
| ru / uk 等西里尔字母 | — | 拉丁转写 | 规则表 | 按词断行 |
| th 泰语 | — | RTGS 转写 | PyThaiNLP（泰语词间无空格，断行要靠分词） | 按词断行 |
| ar / fa 等 | — | 拉丁转写 | 规则表 | **从右往左**，ASS 里要处理 bidi |
| en / fr / de 等 | —（可选 IPA，P2） | — | 空格 | 按词断行，约 42 字符/行 |

首批（M2）只做 ja、zh、en；其余语言按插件逐步加，不影响内核。

### 6.2 振假名生成（已实现，`lang/ja.py`、`lang/ruby.py`）

```
原文 ─► 先按读音覆盖表做最长匹配：读音词典（作品 > 全局）> 术语表读音（大模型给出）
     ─► 其余部分用 Sudachi 分词（core 词典，模式 C），得到表层形 + 读音 + 词性
     ─► 汉字/假名对齐：把词拆成「汉字段 + 假名段」，用正则把读音里的送假名剥掉
            例：「穏やか」读「おだやか」→ [穏|おだ] + [やか]
            例：「引き上げる」→ [引|ひ] き [上|あ] げる
            对不上（熟字训「今朝」「五月雨」）时整词注音：[今朝|けさ]
     ─► 每个词打标：p（专有名词）、num（数词）、guess（专有名词或未登录词、读音来自分词器 → 待确认）
     ─► 输出时按图层的 scope 过滤，再按 script 转成平假名 / 片假名 / 罗马字
```

| scope | 规则 |
|---|---|
| `all` | 全部汉字 |
| `difficult` | 专有名词，以及含有“简单汉字”以外的字的词。简单汉字默认是小学一、二年级的教育汉字（`easy_kanji = grade1 / grade2`），`known_words` 里的词不注 |
| `proper` | 只注专有名词 |
| `first` | 每个词只在第一次出现时注（按时间顺序，每个方案单独计） |

数字（「3人」「五十年」）默认不注音（读音容易错），`numbers = true` 开启。
按 JLPT 级别判断难度需要词表数据，留作以后的改进（计划放在作品 / 用户的“已掌握词表”里一起管理）。

Sudachi 会读错一些词（实测「日の出前」→ 日 / の / 出前（でまえ）、「日本海」→ にっぽん + かい），所以：
- 读音词典优先于分词器，审校界面里点字改读音会写进读音词典；
- 专有名词读音来自分词器时，这条字幕标 `reading_guess`，进入审校队列。

读音词典格式（`%APPDATA%\NyaSubFusion\readings.json`、`作品目录\readings.json`）：

```json
{"entries": [{"lang": "ja", "src": "日本海", "reading": "にほんかい", "proper": true},
             {"lang": "zh-Hans", "src": "长发", "reading": "cháng fà"}]}
```

整行罗马字（`transliterate`）：按词转写，助词は/へ/を 写作 wa/e/o；后缀和接在用言后的助动词、て/で/ば 并到前一个词
（出た → deta，荒れて → arete）；敬称用连字符（Satou-san）；专有名词首字母大写；词尾的「っ」与下一个词的首辅音连写（なかっ|た → nakatta）。长音不加长音符号。

### 6.3 中文拼音 / 注音符号（已实现，`lang/zh.py`）

pypinyin 按词组判断多音字（行长 háng zhǎng、重庆 chóng qìng），逐字对齐；同时取注音符号（轻声点写在音节前）。
声调写法 `tone = mark / number / none`。读音词典里的中文词条通过 pypinyin 的自定义词组生效。整行拼音逐字以空格分隔。

### 6.4 译文的注音

`annotate` 步骤在翻译之后、输出之前：先套用 `edits.json` 里的手动修改，再给原文和每个目标语言的译文按各自的语言包注音，
结果存在 `stages/annotate.json`。它的指纹包含手动修改和读音词典：只改读音词典时只重做注音和输出，不会重新翻译。

## 7. 渲染与排版

### 7.1 排版流程（与格式无关）

```
Cue ─► 按方案取图层（原文/译文/注音/罗马字…）
    ─► 每层：标点规范 → 断行（每行字数、最多行数，超出按“｜”或长度拆成先后几条，时间按字数分配）
    ─► 消除重叠（后一句开始时前一句结束；几乎同时开始的合并）、短字幕延长
    ─► 堆叠：按图层顺序从上到下排（整块底边对齐到字幕位置），量出每层高度（字体度量）
    ─► 避让：与画面原有硬字幕框 / 已放置的说明译文冲突时整体上移
    ─► 说明类：贴在原文画面字旁边（横排上下、竖排左右）
    ─► 输出 Layout：每条字幕的每个图层的绝对坐标、每个注音块的坐标
```

`render/layout.py` 输出的绝对坐标，ASS、硬字幕压制、前端精确预览共用，保证一致。

### 7.2 ASS 中的注音（已实现，`render/ass.py`、`render/fonts.py`）

libass 不支持 ruby，带注音的字幕改为逐块定位（没有注音的字幕仍是普通的一条 `Dialogue`，方便在别的软件里编辑）：

1. 排版把每行拆成块：`[(底字, 注音或 None)]`，相邻的无注音文字合成一块。
2. 字体度量：按字体名（含本地化名，如“微软雅黑”）在系统字体目录里找到字体文件（索引缓存在用户数据目录），用 `fontTools` 读 `hmtx` 宽度。
   libass 与 VSFilter 兼容，把字号当作 `usWinAscent + usWinDescent` 的高度，所以 1 个字体单位 = 字号 / (winAscent + winDescent) 像素。
   找不到字体时按全角 1 个字号、半角 0.55 估算。
3. 带注音的块：底字 `\an2\pos(块中心x, 行底)`，注音 `\an2\pos(块中心x, 行底 − 字号 − 间距)\fs…`。两者用**同一个中心 x**，
   所以即使宽度算得有误差，注音也一定在底字正上方，误差只影响块与块之间的间距。
4. 无注音的块：`\an1\pos(左边x, 行底)`。
5. 注音比底字宽时，可以向两侧的普通文字各探出一个注音字宽，探不下的部分才撑宽底字；两边挨着别的注音时不能探出。
6. 行高 = 底字字号 +（有注音时）注音字号 + 间距；整块高度参与避让计算，与普通字幕一样会避开画面硬字幕。

已用 ffmpeg（libass）把样片烧录截图核对过：振假名、拼音都正好居中在对应底字上方。
限制：播放端必须有同一字体，否则会错位 → 推荐封装字体进 MKV（`ffmpeg -attach`）或直接压制。

其他格式：

| 格式 | 注音 |
|---|---|
| SRT | `漢字(かんじ)` 括号形式，或不输出 |
| VTT | `<ruby>漢字<rt>かんじ</rt></ruby>`（播放器支持度不一） |
| TTML/IMSC | 原生 `tts:ruby` |

## 8. 服务接口（草案）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/projects` | 新建项目（媒体路径、语言对、预设、作品） |
| GET | `/api/projects/{id}` | 项目信息、来源探测结果、各步骤状态 |
| POST | `/api/projects/{id}/run` | 运行 `{until, redo[]}` |
| GET | `/api/projects/{id}/plan?config=…` | 改配置前预演：哪些步骤会重做 |
| GET | `/api/projects/{id}/candidates?src=` | 候选条目（时间轴三路） |
| GET/PATCH | `/api/projects/{id}/cues[/{cid}]` | 字幕条读取、编辑（写入 edits.json） |
| POST | `/api/projects/{id}/cues/{cid}/retranslate` | 单条重译 `{instruction}` |
| POST | `/api/projects/{id}/cues/{cid}/split` / `merge` | 拆分 / 合并 |
| GET | `/api/projects/{id}/layout?profile=&t=` | 某时刻的排版结果（精确预览） |
| POST | `/api/projects/{id}/export` | `{profiles[]}` |
| GET/PUT | `/api/series/{name}` | 作品设置、术语表、读音词典 |
| GET | `/api/schema/config` | 配置 JSON Schema（前端自动生成表单） |
| GET | `/api/langs` | 已安装语言包，及其注音 / 转写选项的 JSON Schema |
| GET/POST | `/api/components` | GPU 组件、模型的安装状态；开始 / 取消下载 |
| POST | `/api/shutdown` | 桌面壳关闭时通知内核优雅退出 |
| GET/PUT | `/api/settings` | 用户全局配置（接口列表、用途分配、镜像）；返回每个接口有没有 Key，不回显 Key |
| PUT/DELETE | `/api/settings/keys/{接口名}` | API Key 存入 / 移出系统凭据库（keyring） |
| POST | `/api/settings/test` | 测试接口连接（发一条很短的请求） |
| POST | `/api/projects/{id}/cues/{cid}/split` · `/merge` · `/api/projects/{id}/cues` · `/edits/undo` · `/edits/redo` | 拆分、与下一条合并、插入、撤销、重做（只重做注音，不会触发重新识别或翻译） |
| GET/PUT | `/api/projects/{id}/glossary` · `/readings` | 术语表（记录来源集数）、读音词典 |
| POST | `/api/projects/{id}/setup` · GET `/api/presets` | 新建向导：按内容预设写 project.toml |
| GET/POST | `/api/queue` | 批量队列状态 / 加入（文件或文件夹 + 未配置视频用的预设、语言、作品、做到哪一步） |
| POST | `/api/queue/{项}/up·down·remove·retry·cancel` · `/api/queue/pause` · `/api/queue/clear` | 调整顺序、移除、重试、取消（步骤之间停下）、暂停 / 继续、清除已完成；进度推送到 `/ws/projects/_queue` |
| WS | `/ws/projects/{id}` | 进度、日志、步骤完成事件 |

## 9. 插件

Python entry points 注册：

```toml
[project.entry-points."nyasub.asr"]
faster_whisper = "nyasub.sources.asr.faster_whisper:Engine"

[project.entry-points."nyasub.lang"]
ja = "nyasub.lang.ja:Japanese"
```

插件声明自己的配置模型（pydantic），界面自动生成设置表单。

## 10. 打包与分发（M4 已实现）

### 10.1 组成

模型和 GPU 运行库都不放进安装包，按需下载。下表是实测体积。

| 部分 | 内容 | 体积 | 获取方式 |
|---|---|---|---|
| 安装包 | Tauri 壳 + `nyasub-core`（Python 运行时、界面、onnxruntime 本体、CTranslate2、OpenCV、Sudachi 词典、OCR 模型 medium + small）+ ffmpeg（共享库版） | 安装包约 337 MB，装好后约 950 MB | NSIS，装在用户目录（不需要管理员权限）；没有显卡也能完整使用 |
| `cuda-asr` 组件 | CUDA 12 cuBLAS + cuDNN 9（语音识别用） | 约 1.3 GB | 项目页「GPU 加速组件」一键下载，或 `nyasub components install cuda-asr` |
| `cuda-ocr` 组件 | onnxruntime CUDA 插件 + CUDA 13 运行库（画面字识别用） | 约 1.2 GB | 同上 |
| 模型 | whisper large-v3 约 3 GB 等 | 按需 | 第一次用到时下载，支持镜像（`[asr] hf_endpoint`） |

- 组件来自 NVIDIA / onnxruntime 发布在 PyPI 上的 wheel，只解出 DLL，装在 `<用户数据目录>\components\<名称>\`；
  依次尝试官方源和国内镜像，也可以用 `--index` 指定。onnxruntime 的 CUDA 插件要和本体放在一起，会复制到内核的 `onnxruntime\capi\`。
- 组件和程序分开：程序更新不会重新下载 CUDA 和模型。
- **GPU 就绪检查**（`nyasub/gpu.py`）：CTranslate2 / onnxruntime 自己报告的“CUDA 可用”只说明编译时带了 CUDA。识别前实际加载一遍需要的 DLL，
  缺了就直接用 CPU，不会在转写到一半时崩溃。
- **没有 GPU 时画面字识别很慢**（实测 720p 每帧：medium 模型 CPU 5 秒、small 模型 CPU 1.2 秒、medium 模型 GPU 0.27 秒），
  所以模型默认 `auto`：有 GPU 用 medium，只有 CPU 时用 small；实际用的模型计入步骤指纹。
- ffmpeg 用 BtbN 的共享库版（`packaging/fetch_ffmpeg.py`）：ffmpeg / ffprobe 共用一套 DLL，约 210 MB（静态版两份约 460 MB），
  并检查包含 libaribcaption（ARIB 字幕）、NVENC、libass。

### 10.2 桌面壳与内核

```
nyasubfusion.exe（Tauri 2）
  ├─ 启动页（本地 splash）
  ├─ 启动 core\nyasub-core.exe serve --port 0 --token <随机> --parent-pid <壳的 PID>（不弹黑窗口）
  ├─ 读到内核 stdout 的 NYASUB_READY {"port": …} → 主窗口跳到 http://127.0.0.1:<port>/?token=…
  │     界面由内核直接提供（同源，没有跨域问题）；token 从地址栏交给前端，所有请求都带上
  └─ 退出：POST /api/shutdown → 等 3 秒 → 强制结束
```

- 内核只监听 127.0.0.1；没有 token 的 `/api` 请求返回 401。
- 正常关窗口：内核先让界面的 WebSocket 断开再退出，实测 0.3 秒；最多等 1 秒（uvicorn 优雅关闭超时）。
- 内核监视壳的进程：壳崩溃或被强制结束时，内核 1 秒左右自己退出（实测），不留孤儿进程。
- 内核日志写到 `%LOCALAPPDATA%\app.nyasubfusion.desktop\logs\core.log`；内核没启动成功时启动页显示日志位置。
- 原生能力通过 Tauri 插件：dialog（选择文件）、窗口拖入文件、single-instance（再次打开时把已有窗口拉到前面）、opener。
  因为界面来自 127.0.0.1，`capabilities/default.json` 用 `remote.urls` 给它开放这些权限。
- 还没做：自动更新（需要签名密钥和发布地址）、托盘、通知、文件关联、关窗口时“后台继续 / 取消”的询问。

### 10.3 开发与构建

| 场景 | 做法 |
|---|---|
| 日常开发 | `python -m nyasub serve` + `web/` 里 `pnpm dev`（Vite 把 `/api` 代理到内核），在浏览器里调试 |
| 调桌面壳 | `desktop/src-tauri` 里 `cargo build`，运行 `target/debug/nyasubfusion.exe`；没有打包好的内核时自动用仓库里的 `.venv`，也可以用 `NYASUB_CORE_CMD` 指定 |
| 打包内核 | `python packaging/fetch_ffmpeg.py`（一次）→ `python packaging/build_core.py`：构建界面 → PyInstaller（onedir，排除 CUDA 库）→ 附带 ffmpeg → 复制到 `desktop/src-tauri/core/` |
| 生成安装包 | `desktop/` 里 `pnpm tauri build` → `desktop/src-tauri/target/release/bundle/nsis/NyaSubFusion_<版本>_x64-setup.exe` |

网络受限时：crates.io 可以换 rsproxy 镜像（在 `desktop/.cargo/config.toml` 里设置，不提交），PyPI 可以换清华 / 阿里镜像。

## 11. 从 subfusion 迁移

| subfusion 模块 | 去向 | 改动 |
|---|---|---|
| `media.py` | `sources/` + `core` | 加代理视频、波形、镜头切换检测 |
| `subs.py` | `sources/subtitle.py` | 基本不变 |
| `asr.py` | `sources/asr/faster_whisper.py` | 语言参数化 |
| `ocr.py` | `sources/ocr/` | 拆出 tracker；ROI 改为多区域 + 排除区 |
| `merge.py` | `fusion/` | 见第 5 节 |
| `translate.py` | `llm/` | prompt 模板化，节目信息来自作品设置；export 模式保留 |
| `layout.py` + `render.py` | `render/` | 图层化；注音排布；真实字体度量替代估算 |
| `pipeline.py` | `core/pipeline.py` | 指纹缓存 |
| `config.py` | `core/config.py` | pydantic 分层配置 |

## 12. 风险

| 风险 | 应对 |
|---|---|
| 振假名读音错误（熟字训、人名） | 多级覆盖 + 审校点字改 + `reading_guess` 标记 |
| ASS 注音错位（字体不一致） | 导出提示、封装字体、压制 |
| 浏览器播不了 TS/HEVC | 导入时生成 H.264 代理 |
| 大模型输出不稳定 | JSON 校验 + 重试 + 回退 + 段级缓存 |
| 长视频时间轴卡顿 | Canvas 渲染 + 视口裁剪 |
| PyInstaller 打包的程序被杀毒软件误报 | 代码签名；用 onedir 而不是单文件 |
| CUDA DLL 版本冲突（用户机器上已有别的 CUDA） | 组件目录隔离 + `add_dll_directory` 显式加载；`doctor` 检查 |
| 老 Windows 10 没有 WebView2 | Tauri 安装包内置 WebView2 引导安装 |
| 小语种注音 / 转写质量参差 | 语言包插件化，首批只承诺 ja / zh / en |
