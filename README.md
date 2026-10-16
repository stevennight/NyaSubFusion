# NyaSubFusion

通用、可视化、可配置的多源字幕生成与翻译工具。

- **多源融合**：自带字幕（外挂 / 内封 / 广播字幕）+ 画面字 OCR + 语音 ASR，有几路用几路
- **高质量翻译**：大模型分段融合纠错 + 翻译，作品级术语表与风格跨集共享
- **图层化输出**：原文、译文、振假名、拼音、罗马字、说明译文各自开关和样式，一次导出多套方案
- **可视化审校**：视频预览 + 多轨时间轴 + 来源对照，点字改读音

前身是命令行工具 `subfusion`（日→中纪录片专用），本项目把它做成通用工作台。

技术栈：Python 内核 + FastAPI 本地服务 + React/TypeScript 界面 + Tauri 2 桌面壳（内核作为 sidecar）。开发阶段直接在浏览器里跑。

## 当前状态：M1（内核 + 命令行）

内核已经从 subfusion 重构成通用版本，命令行可以完整跑通：识别 → 融合 → 大模型融合翻译 → 按输出方案排版导出。
还没做的：振假名 / 拼音 / 罗马字等注音和转写图层（M2）、可视化界面（M3）、桌面打包（M4）。

### 安装（开发环境）

需要 Python 3.11、[uv](https://docs.astral.sh/uv/)、ffmpeg；有 NVIDIA 显卡会快很多。

```bash
uv venv --python 3.11
```

```bash
uv pip install -e ".[asr,ocr,gpu]" pytest --override overrides.txt
```

`overrides.txt` 里写一行 `onnxruntime; sys_platform == 'never'`（faster-whisper 会顺带装 CPU 版 onnxruntime，和 GPU 版冲突）。装好后检查环境：

```bash
.venv/Scripts/python -m nyasub doctor
```

### 使用

```bash
.venv/Scripts/python -m nyasub run 视频.ts --target zh-Hans,en --series 作品名
```

- 配置见 [config.example.toml](config.example.toml)：语言、识别参数、大模型接口、样式、输出方案（图层 = 角色 × 语言）。
- 没配大模型接口时自动导出翻译任务（`<视频名>.nyasub/jobs/TASK.md`），交给 Claude Code 完成后再运行同一条命令即可导入。
- 每一步的结果和指纹存在 `<视频名>.nyasub/stages/`：改了哪个参数只重做受影响的步骤，大模型请求也有缓存。`--redo 步骤` 强制重做，`--until fuse` 只做到原文合并。
- `<视频名>.nyasub/edits.json` 里的手动修改会在输出时套用（以后由审校界面写入）。

### 测试

```bash
.venv/Scripts/python -m pytest
```

端到端测试用 40 秒的样片（配音 + 硬字幕 + 部分外挂字幕）和模拟大模型；标了 `slow` 的那条会真的跑语音和画面字识别。

## 文档与原型

| 文件 | 内容 |
|---|---|
| [docs/PRD.md](docs/PRD.md) | 产品需求：定位、场景、核心概念、功能清单（P0/P1/P2）、里程碑 |
| [docs/DESIGN.md](docs/DESIGN.md) | 技术设计：架构、数据模型、流水线与指纹缓存、融合算法、振假名/拼音、ASS 注音排布、接口 |
| [prototype/index.html](prototype/index.html) | 可交互原型（直接用浏览器打开，无需构建） |

原型里可以试：

- **审校**：播放、点时间轴/列表跳转、J/K 切换、显示 OCR 识别框、点原文汉字改读音、重新翻译
- **图层样式**：切换方案（中日双语 / 中英双语 / 日语学习 / 纯中文 / 拼音）；每层选语言，注音菜单跟着语言变（日语：振假名；中文：拼音、注音符号；英语：无）；增删译文层和转写层，调整顺序、显隐、字体、振假名范围与文字，右侧实时预览
- **流程**：改任何参数后，图上标出需要重做的步骤；在样帧上拖动框选 OCR 识别区 / 排除区
- **导出**：勾选多个方案一次导出
