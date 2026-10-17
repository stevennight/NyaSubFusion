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
    if getattr(sys, "frozen", False):  # 打包后：随程序附带的 ffmpeg
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        if (base / "ffmpeg").is_dir():
            os.environ["PATH"] = str(base / "ffmpeg") + os.pathsep + os.environ.get("PATH", "")
    dirs = set()
    if os.name == "nt":
        # pip 安装的 nvidia-* 包、以及 GPU 组件目录里的 DLL
        roots = [p for p in sys.path if p.endswith("site-packages")]
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
        # 只有找到了 CUDA 运行库才预加载，否则 onnxruntime 会刷一屏“加载失败”
        if hasattr(ort, "preload_dlls") and os.name == "nt" and dirs:
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


def on_event(fn):
    """订阅事件（服务层用来推送到界面）。fn(event: dict) 会在发出事件的线程里调用。"""
    _listeners.append(fn)


def off_event(fn):
    if fn in _listeners:
        _listeners.remove(fn)


def emit(event: dict):
    for fn in list(_listeners):
        try:
            fn(event)
        except Exception:
            pass


def log(*args):
    msg = " ".join(str(a) for a in args)
    try:
        print("[nyasub]", msg, flush=True)
    except (OSError, ValueError):
        pass  # 标准输出已经关了（比如桌面壳退出了），不影响运行
    emit({"type": "log", "msg": msg})


_job = None


def kill_children_on_exit():
    """Windows：把自己放进一个“关闭即结束”的作业对象。内核退出（包括被桌面壳强行结束、崩溃）时，
    它启动的 ffmpeg 之类的子进程也一起结束，不会留在后台继续转码。"""
    global _job
    if os.name != "nt" or _job is not None:
        return
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("Io", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if job and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) \
                and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            _job = job  # 句柄一直开着；进程结束时系统关闭它，作业里的进程随之结束
    except Exception:
        pass
