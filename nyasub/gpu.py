"""GPU 是否真的能用：实际尝试加载需要的 DLL。

CTranslate2 和 onnxruntime 自己报告的“CUDA 可用”只说明编译时带了 CUDA，不代表运行库在：
缺了 cuBLAS / cuDNN 时，语音识别会在转写途中才崩溃。所以识别前先查一遍，缺了就直接用 CPU。
"""
import ctypes
import os
from functools import lru_cache
from pathlib import Path

ASR_DLLS = ("cublas64_12.dll", "cudnn64_9.dll")      # CTranslate2 4.x（CUDA 12）
OCR_DLLS = ("cudart64_13.dll", "cublas64_13.dll", "cudnn64_9.dll")  # onnxruntime-gpu 1.2x+（CUDA 13）


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
