# PyInstaller 配置：把内核打成 onedir 的 nyasub-core（给 Tauri 桌面壳当后台程序）。
# 用 packaging/build_core.py 调用，不要直接运行。
#
# 基础包只带 CPU 能力：CUDA 运行库、onnxruntime 的 CUDA / TensorRT 插件都不打进去，
# 由“GPU 加速组件”按需下载（见 nyasub/components.py）。
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 （PyInstaller 注入）
WEB_DIST = ROOT / "web" / "dist"
if not (WEB_DIST / "index.html").exists():
    raise SystemExit("先构建界面：cd web && pnpm build")

datas = [(str(WEB_DIST), "web")]
for pkg in ("sudachipy", "sudachidict_core", "pypinyin", "faster_whisper", "rapidocr"):
    datas += collect_data_files(pkg)
# OCR 模型：medium（有 GPU 时）和 small（只有 CPU 时）都带上，离线也能用

binaries = collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("av")

hiddenimports = (collect_submodules("uvicorn") + collect_submodules("nyasub")
                 + ["sudachipy", "sudachidict_core", "pypinyin", "tomli_w", "websockets", "httptools", "watchfiles"])

EXCLUDE_DLLS = ("onnxruntime_providers_cuda", "onnxruntime_providers_tensorrt", "cublas", "cudnn", "cudart",
                "cufft", "curand", "nvrtc", "nvjitlink", "nvblas")

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "core_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "nvidia", "torch", "PyInstaller"],
    noarchive=False,
)
# 去掉 CUDA 相关的库（它们来自 nvidia-* 包或 onnxruntime-gpu），留给 GPU 组件
a.binaries = [b for b in a.binaries if not any(x in os.path.basename(b[0]).lower() for x in EXCLUDE_DLLS)]
a.datas = [d for d in a.datas if not d[0].replace("\\", "/").startswith("nvidia/")]

pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [], exclude_binaries=True, name="nyasub-core", console=True, upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="nyasub-core", upx=False)  # noqa: F821
