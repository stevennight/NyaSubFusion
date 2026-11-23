"""GPU / 硬件加速是否真的能用：
- Windows：实际尝试加载 CUDA DLL（CTranslate2 / onnxruntime 自己报告的“CUDA 可用”只说明编译时带了 CUDA，缺运行库时会在运行途中崩溃）。
- macOS（Apple Silicon）：检测 VideoToolbox（视频编解码）、CoreML（ONNX Runtime 人声分离）和可选的 mlx-whisper（语音识别）。
"""
import ctypes
import importlib.util
import os
import platform
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ASR_DLLS = ("cublas64_12.dll", "cudnn64_9.dll")      # CTranslate2 4.x（CUDA 12）
OCR_DLLS = ("cudart64_13.dll", "cublas64_13.dll", "cudnn64_9.dll")  # onnxruntime-gpu 1.2x+（CUDA 13）


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_apple_silicon() -> bool:
    return is_macos() and platform.machine().lower() in ("arm64", "aarch64")


@lru_cache(maxsize=1)
def apple_chip_name() -> str | None:
    if not is_macos():
        return None
    try:
        r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return "Apple Silicon" if is_apple_silicon() else "macOS"


def _loadable(names):
    if os.name != "nt":
        return True  # 其他平台交给库自己处理
    for n in names:
        try:
            ctypes.WinDLL(n)
        except OSError:
            return False
    return True


@lru_cache(maxsize=1)
def asr_ready() -> bool:
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() < 1:
            return False
    except Exception:
        return False
    return _loadable(ASR_DLLS)


@lru_cache(maxsize=1)
def ocr_ready() -> bool:
    try:
        import onnxruntime

        if "CUDAExecutionProvider" not in onnxruntime.get_available_providers():
            return False
        capi = Path(onnxruntime.__file__).parent / "capi"
        if os.name == "nt" and not (capi / "onnxruntime_providers_cuda.dll").exists():
            return False
    except Exception:
        return False
    return _loadable(OCR_DLLS)


@lru_cache(maxsize=1)
def ort_coreml_ready() -> bool:
    if not is_macos():
        return False
    try:
        import onnxruntime

        return "CoreMLExecutionProvider" in onnxruntime.get_available_providers()
    except Exception:
        return False


def vocals_ready() -> bool:
    """人声分离能否用硬件加速（Windows/Linux CUDA，或 macOS CoreML）。"""
    return ocr_ready() or ort_coreml_ready()


def ort_providers(use_gpu: bool) -> list[str]:
    """按平台返回 ONNX Runtime 推理后端列表。"""
    if not use_gpu:
        return ["CPUExecutionProvider"]
    if ocr_ready():
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    if ort_coreml_ready():
        return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


@lru_cache(maxsize=1)
def mlx_whisper_ready() -> bool:
    if not is_apple_silicon():
        return False
    return importlib.util.find_spec("mlx_whisper") is not None


@lru_cache(maxsize=1)
def videotoolbox_ready() -> bool:
    if not is_macos():
        return False
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and "h264_videotoolbox" in r.stdout
    except Exception:
        return False
