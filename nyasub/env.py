"""运行环境：控制台 UTF-8、CUDA DLL 路径、日志、用户数据目录。"""
import glob
import os
import sys
from pathlib import Path

_done = False


def setup():
    global _done
    if _done:
        return
    _done = True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("ORT_LOG_SEVERITY_LEVEL", "3")  # 屏蔽 onnxruntime 的噪声日志
    if os.name == "nt":
        # pip 安装的 nvidia-* 包、以及 GPU 组件目录里的 DLL
        roots = [p for p in sys.path if p.endswith("site-packages")]
        dirs = set()
        for sp in roots:
            dirs |= {os.path.dirname(p) for p in glob.glob(os.path.join(sp, "nvidia", "**", "*.dll"), recursive=True)}
        comp = home() / "components"
        if comp.is_dir():
            dirs |= {os.path.dirname(p) for p in glob.glob(str(comp / "**" / "*.dll"), recursive=True)}
        for d in sorted(dirs):
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    try:
        import onnxruntime as ort

        ort.set_default_logger_severity(3)
        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls(directory="")
    except Exception:
        pass


def home() -> Path:
    """用户数据目录：全局配置、作品（series）、GPU 组件。可用 NYASUB_HOME 覆盖。"""
    if os.environ.get("NYASUB_HOME"):
        return Path(os.environ["NYASUB_HOME"])
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "NyaSubFusion"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nyasub"


_listeners = []


def on_log(fn):
    """订阅日志（服务层用来推送到界面）。"""
    _listeners.append(fn)


def log(*args):
    msg = " ".join(str(a) for a in args)
    print("[nyasub]", msg, flush=True)
    for fn in _listeners:
        fn(msg)
