# macOS（Apple Silicon）适配设计

本文档记录 NyaSubFusion macOS 版的跨平台架构设计与硬件加速方案（分支 `feat/macos-support`）。

## 1. 目标与原则

- **零回归**：不破坏现有 Windows 架构、配置兼容性和打包流程。
- **充分利用 Apple Silicon 硬件**：
  - 视频编解码：接入 **Apple VideoToolbox**（H.264 / HEVC 8-bit & 10-bit 硬件编解码）。
  - 人声分离（MDX-Net）：通过 `onnxruntime` 原生自带的 **CoreML Execution Provider** 调度 GPU / Neural Engine。
  - 语音识别（ASR）：支持可选的 **`mlx-whisper`** 后端（Apple MLX GPU 加速），未安装时平滑回退到 `faster-whisper` CPU `int8`。
- **原生体验**：Tauri 2 桌面壳在 macOS 上使用原生窗口控制（红绿灯 + `TitleBarStyle::Overlay`）和标准数据目录（`~/Library/Application Support/NyaSubFusion`）。

---

## 2. 硬件加速矩阵

| 模块 | Windows（NVIDIA） | macOS（Apple Silicon） | 兜底方案 |
| :--- | :--- | :--- | :--- |
| 硬字幕压制编码 | `h264_nvenc` / `hevc_nvenc` | `h264_videotoolbox` / `hevc_videotoolbox` | `libx264` / `libx265` |
| 硬字幕压制解码 | `-hwaccel cuda` + `scale_cuda` / `yadif_cuda` | `-hwaccel videotoolbox` + CPU 滤镜链（统一内存） | CPU 解码 |
| HLS 预览转码 | `cuda` + `h264_nvenc` | `videotoolbox` + `h264_videotoolbox` | CPU + `libx264` |
| OCR 逐帧抽帧 | `cuda` + `scale_cuda` | `-hwaccel videotoolbox` + CPU 缩放 | CPU 解码 |
| 人声分离（MDX-Net） | `onnxruntime` CUDA EP | `onnxruntime` CoreML EP | `onnxruntime` CPU EP |
| 画面字识别（RapidOCR） | `onnxruntime` CUDA EP | CPU（避免误报缺 CUDA 警告） | CPU |
| 语音识别（ASR） | `faster-whisper` CUDA | 可选 `mlx-whisper`（Metal） | `faster-whisper` CPU `int8` |

---

## 3. 关键模块改动

### 3.1 视频编解码（VideoToolbox）

- **`nyasub/core/config.py`**：`VideoOutCfg.encoder` 增加 `"h264_videotoolbox"` 和 `"hevc_videotoolbox"`。
- **`nyasub/render/video.py`**：
  - `vt_quality(q)`：将 CRF/CQ（`0..51`，越小画质越好）映射到 VideoToolbox `-q:v`（`1..100`，越大画质越好）：`clamp(round(100 - q * 1.8), 1, 100)`（如 `q=20 → 64`，`q=23 → 59`，`q=18 → 68`）。
  - `pick_encoders()`：`encoder="auto"` 时在 `sys.platform == "darwin"` 下优先选择 `hevc_videotoolbox` / `h264_videotoolbox`，失败自动回退 `libx265` / `libx264`。
  - `_resolve_bit_depth()`：`hevc_videotoolbox` 支持 10-bit（`p010le` + `-profile:v main10`）；`h264_videotoolbox` 与 `h264_nvenc` 一样自动降级 8-bit `yuv420p`。
  - `_burn_vf()`：VideoToolbox 硬解使用 `-hwaccel videotoolbox`，滤镜链走 CPU（`bwdif`、`scale`、`zscale`、`subtitles`），失败时自动回退 CPU 解码 + VideoToolbox 编码，再回退纯 CPU。
- **`nyasub/sources/media.py`**：
  - `proxy_attempts()`：macOS 下依次尝试 `VideoToolbox 硬件解码 + 编码` → `CPU 解码 + VideoToolbox` → `CPU 解码 + x264`。
  - `_frame_cmd()`：macOS 下 `hw=True` 时使用 `-hwaccel videotoolbox` 硬件解码。

### 3.2 GPU 检测、AI 推理与环境

- **`nyasub/gpu.py`**：增加 `is_macos()`、`is_apple_silicon()`、`apple_chip_name()`、`ort_coreml_ready()`、`ort_providers(use_gpu)`、`mlx_whisper_ready()`、`videotoolbox_ready()`。
- **`nyasub/sources/vocals.py`**：通过 `gpu.ort_providers()` 自动在 macOS 上启用 `CoreMLExecutionProvider`。
- **`nyasub/sources/asr.py`**：
  - macOS 上不再误报“缺少 NVIDIA CUDA / 请安装 GPU 加速组件”。
  - 若安装了 `mlx-whisper` 且在 Apple Silicon 上，本地 ASR 自动走 MLX GPU 推理（返回一致的段落与逐字时间戳结构）；未安装时自动回退 `faster-whisper` CPU `int8`。
- **`nyasub/sources/ocr.py`**：macOS 上跳过 CUDA 缺失告警，按实际可用加速选择模型尺寸。
- **`nyasub/env.py`**：macOS 下 `home()` 默认使用 `~/Library/Application Support/NyaSubFusion`。
- **`nyasub/components.py`**：macOS 下 `manifest()` 返回空（无需下载 CUDA DLL），`gpu_info()` 返回芯片名、VideoToolbox / CoreML / MLX 状态及 `platform` 字段。

### 3.3 桌面壳、前端与打包

- **`desktop/src-tauri/src/lib.rs`**：跨平台查找内核二进制（`nyasub-core` / `.venv/bin/python`）。
- **`desktop/src-tauri/tauri.macos.conf.json`**：macOS 专属配置覆盖（`decorations: true`、`titleBarStyle: "Overlay"`、`hiddenTitle: true`、`targets: ["dmg", "app"]`、`icon.icns`）。
- **`web/src/`**：
  - `Output.tsx`：编码器下拉框支持 VideoToolbox 选项；字体候选列表补充 macOS 常用中日文字体（PingFang SC、Hiragino Sans 等）。
  - `GpuComponents.tsx`：macOS 下展示 Apple Silicon 硬件加速状态，隐藏 NVIDIA CUDA 下载项。
  - `WindowControls.tsx` & `styles.css`：macOS 下隐藏右侧 Windows 风格最小化/最大化/关闭按钮，并在顶栏左侧为原生红绿灯预留空间。
- **`pyproject.toml`**：
  - `override-dependencies` 改为 `["onnxruntime; sys_platform == 'darwin'"]`（macOS 没有 `onnxruntime-gpu`，保留原生带 CoreML 的 `onnxruntime`）。
  - 增加可选依赖组 `mlx = ["mlx-whisper>=0.4"]`。
- **`packaging/`**：
  - `build_core.py` 支持 macOS / Unix 打包（复制 `ffmpeg` / `ffprobe` 及相关 `.dylib` 并确保可执行权限）。
  - `fetch_ffmpeg.py` 增加 macOS 检出与能力校验（`libaribcaption`、`h264_videotoolbox`、`hevc_videotoolbox`、`libass`）。
  - 新增 `packaging/build_macos.sh` 一键本地构建脚本。

---

## 4. macOS 构建与验证步骤（在 Apple Silicon Mac 上）

```bash
# 1. 安装依赖（可选加上 --extra mlx 启用 MLX 语音识别加速）
uv sync --extra asr --extra ocr --extra lang --extra server --extra mlx

# 2. 确保 ffmpeg 包含 libaribcaption + videotoolbox + libass
#    （Homebrew 默认 ffmpeg 若未编译 libaribcaption，可使用自定义 tap 或源码编译）
.venv/bin/python packaging/fetch_ffmpeg.py

# 3. 运行单元测试
.venv/bin/pytest -q

# 4. 构建 macOS .app / .dmg
bash packaging/build_macos.sh
```
