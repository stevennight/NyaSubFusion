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
  media/              # meta.json、audio.wav、preview/（HLS 预览）、peaks.json、shots.json（镜头切换点）
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

### 5.0 只有语音时：人声分离和时间修复

没有字幕、语音是主要来源时（`asr.separate = "auto"`，动画预设为 on），识别前先做人声分离（`sources/vocals.py`）：
MDX-Net（UVR 的 Kim_Vocal_2，ONNX，64 MB，首次下载到 `<用户数据目录>/models/`），n_fft 7680 / hop 1024 / 3072 个频点，
每块约 5.9 秒、两边各多算 n_fft/2 再裁掉；ffmpeg 管道进出，内存不随时长增长。有 CUDA 用显卡，CPU 上约为节目时长的 1/5。
分离后的人声存 `media/vocals.wav`（记着视频文件和模型，没变就复用）；背景音乐去掉了，人声门槛自动放到不高于 0.15。

语音段时间修复（`sources/asr.repair`，在使用语音结果时做，不用重新识别）：人声检测把一大段音乐和台词连在一起时，
第一个字的时间停在音乐开头（“気”161.5 秒 → “持ちいい”175.2 秒），整句提前十几秒。段内相邻两字间隔超过 2 秒就拆开，
拆出来 ≤ 2 个字的碎片并到空档另一边。另外传 `hallucination_silence_threshold=2` 让 faster-whisper 跳过长静音里的幻觉。

实测（《東方八犬異聞》第 1 话，24 分钟，large-v3，以字幕组中文硬字幕的时间为参照，只用来打分）：

| | 完全漏掉的台词 | 起点 1.5 秒内对上 | 提前 2 秒以上的段 |
|---|---|---|---|
| 原音频（门槛 0.1） | 33 | 69% | 8 |
| 原音频 + 时间修复 | 33 | 70% | 0 |
| 人声分离 + 时间修复（门槛 0.1） | 22 | 74% | 1 |
| 再加补识别 | 13 | 72% | 1（另有 4 段是歌词和片头片尾字幕重叠，不算错） |

补识别（`sources/asr.py _fill_gaps`，只在分离出的人声上做）：人声检测（Silero VAD）不把哭喊、尖叫、歌声当成说话，
这些地方整段被跳过，但关掉人声检测、切成短片段单独识别，large-v3 其实听得出来。做法：人声轨按 50 毫秒算音量，
比底噪高 18 dB（且不低于 -45 dB）、又没被任何语音段覆盖的地方，合并成不超过 20 秒的片段，关掉人声检测再识别一遍。
补出来的段标 `second_pass`，提示词里注明“可能是哭喊、歌声，也可能是噪音或幻觉”，由大模型取舍；
整段等于常见幻觉句（“お疲れ様でした”“それではまた”…）、不到 0.4 秒的碎片、平均对数概率低于 -0.8 的先丢掉。
这一集补出 26 段：开头孩子哭喊的 4 句、片头 / 片尾曲歌词、剧中唱的歌的一句、下集预告。

剩下漏掉的：剧中唱的诗大半（古文 + 唱腔）、只有一两个字的呼喊。

模型对比（同一集，人声分离 + 补识别 + 时间修复）：

| 模型 | 完全漏掉 | 起点 1.5 秒内对上 | 超过 8 秒的段 | 用时（显卡） | 文字 |
|---|---|---|---|---|---|
| large-v3 | 9 | 73% | 6 | 181 s | 准 |
| large-v3-turbo | 6 | 82% | 3 | 51 s | 和 large-v3 差不多 |
| kotoba-whisper v2.0 | 8 | 70% | 9 | 28 s | 不少句子听错、时间对错 |
| anime-whisper | 5 | 69% | 18 | 51 s | 哭喊最好，普通台词错得多；不分段 |

动画预设用 large-v3-turbo。模型列表和下载在 `sources/asr_models.py`：先找本地，Hugging Face 下载失败就从 ModelScope 下载
（文件清单按 ModelScope 接口取，词表文件名各模型不同）。kotoba 这类蒸馏模型（解码器 2 层）的 `alignment_heads` 指向不存在的层，
算逐字时间时 CTranslate2 会直接崩溃（segfault），加载前改成最后一层的全部注意力头。
anime-whisper 只有 Transformers 格式，要 PyTorch 才能转换，没放进列表。

### 5.1 没人说出来的画面字（菜名、标题、地名标签）

例：旁白在说“郷土料理の治部煮風に仕立てた鴨鍋。”，同时画面上打出菜名“鴨治部すき”。大模型容易把它们合成一条，结果菜名的译文
跟着旁白提前出现、用台词样式显示（明明没人说），还和下一句时间重叠。处理分三层：

1. 规则融合只记事实，不下结论：画面字出现期间，字幕大半时间在说别的话（去掉“（取材者）”这类说话人标注、
   “（笑い声）”“♬〜”这类音效 / 音乐说明后还有字），而且和语音也对不上 → 标 `over_speech`。
   台词还是说明仍交给大模型按内容判断（旁白同时另一个人说的话也会有这个标记）。
2. 提示词：这类条目标“同一时间字幕在说别的话”；规则写明没人念出来的标题、菜名、人名条、地名不能和台词合成一条。
3. 翻译结果保护（`split_unspoken`，接口和导出任务两种方式都会走）：一条 cue 里既有台词又有带 `over_speech` 的画面字，
   而且这条画面字和台词内容对不上时，按“｜”分段找出和它整体对得上的连续几段（大模型只留了一部分、
   或者两行的标签被分成两段也能认出），拆成独立的 info cue，时间、位置都用画面字自己的。
   台词里本来就说到这个词的那一段不会被拆走；对不上段数就保持原样。
   半句字幕、半句画面字（同一句话）不会被拆：画面字出现时官方字幕一般不重复显示，不会有 `over_speech`。

说明类译文的位置（`render/ass.py _info_spot`）：位置框取这条 cue 所有画面字来源的并集；依次试贴着原文、原文另一侧、
画面顶部、画面底部，取第一个不出画面、也不压住同一时间其他画面字的位置（标题卡的大字常常只识别出一部分框）。

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
限制：播放端必须有同一字体，否则会错位 → 封装 MKV 时嵌入字体，或直接压制（见 7.3）。

字体查找：同一个名字可能对应多个字形（“Yu Gothic”既是常规体的家族名，也是细体、粗体的排版家族名），
按 libass 的选法取：家族名相同的常规体 > 家族名 > 全名 > 排版家族名，同级非粗体优先。

### 7.3 输出视频（已实现，`render/video.py`）

`[render.video]`：`mode = "mux" | "burn"` 时在排版输出后多一步 `mux` / `burn`（同样有指纹：视频文件 + 实际用到的字幕内容 + 相关配置，
没变就不重做）；界面和接口也可以随时单独导出。

- **字体**：解析 ASS 的样式和行内 `\fn` `\b` `\r`，得到每种字体（区分粗体）实际用到的字；用 fontTools 取子集
  （保留全部 name 记录和排版特性，去掉 hinting），通常几百 KB。字体许可 `fsType` 为“受限”的跳过，“不允许取子集”的嵌入完整字形。
  字体集合（.ttc）只取用到的那一个字形。
- **封装 MKV**（`<名>.sub.mkv`）：视频、音频原样复制；可选保留原有字幕轨（mov_text 转成 SRT，ARIB 之类不能放进 MKV 的丢掉）；
  新字幕轨标 ISO 639-2 语言和图层语言组成的名字（如“简体中文 / 日语”），第一条设为默认；字体用 `-attach` 加 mimetype。
- **压制**（`<名>.hardsub.mp4`）：字幕和字体先复制到工作目录，ffmpeg 在那里运行（`subtitles=sub.ass:fontsdir=fonts`，
  避免在滤镜参数里转义 Windows 路径）。滤镜：隔行 → `bwdif`；非方形像素或指定了高度 → 缩放成方形像素（否则 libass 画出的字会变形）；
  最后 `subtitles`。编码 `auto` = 先 `h264_nvenc`，一开始就失败（没有 N 卡）再用 `libx264`；音频是 AAC / MP3 / AC3 时直接复制，否则转 AAC。
- **进度与取消**：`ffmpeg -progress pipe:1` 读出处理到的时间，推 `{"type": "progress", "stage", "done", "total"}`；
  取消时结束 ffmpeg 并删掉 `.part` 半成品，步骤状态记为 cancelled。
- 加文件夹时跳过 `.sub.mkv` / `.hardsub.mp4`，不会把输出当成新视频再处理一遍。
- 4K HEVC 之类 CPU 解码很慢的源：压制时先用显卡解码（`-hwaccel cuda`），反交错、缩放、转 8 位都在显卡上做
  （`yadif_cuda`、`scale_cuda=…:format=yuv420p`），`hwdownload` 后再画字幕；不行再走 CPU 解码。
  封装时日本广播的 `aac_latm` 音频转成普通 AAC（很多播放器不认 MKV 里的 LATM），其他照旧复制。

### 7.4 界面里的预览视频

浏览器 / WebView2 能直接放的（MP4 / WebM 里的 H.264、VP9、AV1）直接放原文件。其他格式（TS / M2TS / MKV、HEVC、MPEG-2…）
转成 `media/preview/` 下的 HLS 分段：540p、8 位 H.264、每段 4 秒，帧率高于 30 的减半。播放列表是 event 类型，
**转出第一段（约 1 秒）就能开始播放**，界面用 hls.js 播放、自动刷新播放列表，可以看、可以拖动到已转好的位置；
转完写上 `#EXT-X-ENDLIST`，变成可以随意拖动的普通视频。

- 做法按顺序试：显卡解码 + `scale_cuda` + NVENC → CPU 解码 + NVENC → CPU 解码 + x264，前一种一开始就失败才换下一种。
  4K HEVC 10 位 60p 大约 7 倍速（一小时的节目约 9 分钟转完）。NVENC 要加 `-forced-idr 1`，否则不按 4 秒切段。
- `GET /media/source` 告诉界面怎么播放（direct / mp4 / hls）和转换进度；没转过、或上次没转完程序就关了，就从头转。
  失败时返回原因，界面上显示。
- 为什么不“直接播放原文件”：WebView2 不支持 M2TS / TS 容器；4K 10 位 HEVC 要看显卡和系统的 HEVC 解码支持；
  只换容器不转码又要多占一份原文件大小的磁盘（这一集 11 GB）。540p 预览只有几百 MB，拖动也快。
- 旧版的 `proxy.mp4`（遇到 10 位源会转出浏览器放不了的 High 10）打开时删掉；v2 的 `proxy2.mp4` 已经能放，继续用。

内核启动时把自己放进 Windows 作业对象（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）：内核退出（包括被桌面壳强行结束）时，
正在跑的 ffmpeg 一起结束，不会留在后台。

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
| POST | `/api/projects/{id}/export_video` | `{mode: "mux" \| "burn", profiles[]}`：后台封装 / 压制，进度事件推到项目频道 |
| GET | `/api/projects/{id}/media/source` · `/media/hls/{index.m3u8 \| seg_NNNNN.ts}` | 界面怎么播放：直接放原视频，或边转边看的 HLS 预览（返回转换进度） |
| POST | `/api/projects/{id}/cancel` | 取消正在运行的任务：语音识别每段、画面字每帧、大模型每次请求前、ffmpeg 运行中都会检查（`env.check_cancel`），几秒内停下 |
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
| POST | `/api/queue/{项}/up·down·remove·retry·cancel` · `/api/queue/pause` · `/api/queue/clear` | 调整顺序、移除、重试、取消（几秒内停下）、暂停 / 继续、清除已完成；进度推送到 `/ws/projects/_queue` |
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
