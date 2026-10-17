"""可选组件（GPU 加速）：按需下载，不放进安装包。

组件来自 NVIDIA / onnxruntime 发布在 PyPI 上的 wheel，只解出里面的 DLL：
- cuda-asr：语音识别用（faster-whisper / CTranslate2 需要 CUDA 12 的 cuBLAS、cuDNN 9），约 1.3 GB
- cuda-ocr：画面字识别用（onnxruntime 的 CUDA 插件 + 它需要的 CUDA 13 运行库），约 1.2 GB

装在 <用户数据目录>/components/<名称>/，env.setup() 启动时把这些目录加进 DLL 搜索路径。
onnxruntime 的 CUDA 插件必须和 onnxruntime 本体放在一起，所以 cuda-ocr 会把插件复制到 onnxruntime/capi/
（安装包默认装在用户目录，可写）。
"""
import fnmatch
import html.parser
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import env
from .core.store import load_json, save_json

DEFAULT_INDEX = "https://pypi.org/simple"
MIRRORS = ["https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.aliyun.com/pypi/simple"]


def _ort_version():
    try:
        import onnxruntime

        return onnxruntime.__version__
    except Exception:
        return None


def manifest():
    ort = _ort_version()
    return {
        "cuda-asr": {
            "label": "语音识别 GPU 加速",
            "detail": "CUDA 12 cuBLAS + cuDNN 9（faster-whisper 用），约 1.3 GB",
            "packages": [("nvidia-cublas-cu12", ""), ("nvidia-cudnn-cu12", ">=9,<10")],
        },
        "cuda-ocr": {
            "label": "画面字识别 GPU 加速",
            "detail": f"onnxruntime {ort or '?'} CUDA 插件 + CUDA 13 运行库，约 1.2 GB",
            "packages": [("nvidia-cuda-runtime", "~=13.0"), ("nvidia-cublas", ">=13,<14"), ("nvidia-cudnn-cu13", "~=9.0"),
                         ("nvidia-cufft", "~=12.0"), ("nvidia-curand", "~=10.0"), ("nvidia-nvjitlink", ">=13,<14"),
                         ("nvidia-cuda-nvrtc", "~=13.0")]
                        + ([("onnxruntime-gpu", f"=={ort}", ["onnxruntime/capi/onnxruntime_providers_cuda.dll",
                                                              "onnxruntime/capi/onnxruntime_providers_shared.dll"])] if ort else []),
        },
    }


def root():
    return env.home() / "components"


def status():
    """[{name, label, detail, installed, version_files, size_mb}] + 显卡信息。"""
    out = []
    for name, m in manifest().items():
        rec = load_json(root() / name / "component.json")
        out.append({"name": name, "label": m["label"], "detail": m["detail"], "installed": bool(rec),
                    "files": len(rec["files"]) if rec else 0, "size_mb": rec.get("size_mb") if rec else None})
    return {"gpu": gpu_info(), "components": out}


def gpu_info():
    from . import gpu

    info = {"cuda_devices": 0, "name": None, "ort_cuda": gpu.ocr_ready(), "asr_cuda": gpu.asr_ready()}
    try:
        import ctranslate2

        info["cuda_devices"] = ctranslate2.get_cuda_device_count()
    except Exception:
        pass
    try:
        import subprocess

        r = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            info["name"] = r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return info


# ---------------- 下载 ----------------


class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def _pick_wheel(index, package, spec):
    """从 simple 索引里挑出满足版本要求、能在 Windows x64 上用的最新 wheel。返回 (版本, 地址)。"""
    from packaging.specifiers import SpecifierSet
    from packaging.utils import parse_wheel_filename

    url = f"{index.rstrip('/')}/{package}/"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "nyasub"}), timeout=30) as r:
        p = _Links()
        p.feed(r.read().decode("utf-8", "replace"))
    best = None
    specs = SpecifierSet(spec) if spec else SpecifierSet()
    for href in p.links:
        fname = urllib.parse.unquote(href.split("#")[0].rsplit("/", 1)[-1])
        if not fname.endswith(".whl"):
            continue
        try:
            _, ver, _, tags = parse_wheel_filename(fname)
        except Exception:
            continue
        if ver.is_prerelease or ver not in specs:
            continue
        plats = {t.platform for t in tags}
        if not (plats & {"win_amd64", "any"}):
            continue
        if any(t.interpreter.startswith("cp") and t.interpreter not in ("cp311", "cp3") for t in tags) and \
                not any(t.interpreter in ("cp311", "py3") or t.abi == "abi3" for t in tags):
            continue
        if best is None or ver > best[0]:
            best = (ver, urllib.parse.urljoin(url, href.split("#")[0]))
    if not best:
        raise RuntimeError(f"索引里找不到 {package}{spec} 的 Windows 版本")
    return str(best[0]), best[1]


def _download(url, dest, progress):
    req = urllib.request.Request(url, headers={"User-Agent": "nyasub"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0.0
        while True:
            buf = r.read(1 << 20)
            if not buf:
                break
            f.write(buf)
            done += len(buf)
            if time.time() - last > 0.5:
                last = time.time()
                progress(done, total)
        progress(done, total)


def install(name, index=None):
    """下载并安装组件。index 为空时依次尝试官方源和国内镜像。"""
    m = manifest().get(name)
    if not m:
        raise ValueError(f"没有这个组件：{name}")
    indexes = [index] if index else [DEFAULT_INDEX] + MIRRORS
    dest = root() / name
    staging = Path(tempfile.mkdtemp(prefix=f"nyasub-{name}-"))
    files, total_size = [], 0
    try:
        for pkg in m["packages"]:
            package, spec = pkg[0], pkg[1]
            patterns = pkg[2] if len(pkg) > 2 else ["nvidia/*.dll", "nvidia/**/*.dll"]
            ver, url, err = None, None, None
            for idx in indexes:
                try:
                    ver, url = _pick_wheel(idx, package, spec)
                    break
                except Exception as e:
                    err = e
                    env.log(f"  {idx} 不可用（{str(e)[:80]}），换下一个源")
            if not url:
                raise RuntimeError(f"下载 {package} 失败：{err}")
            env.log(f"下载 {package} {ver}")
            whl = staging / url.rsplit("/", 1)[-1]

            def progress(done, total, package=package):
                env.emit({"type": "component", "name": name, "package": package, "done": done, "total": total})
            _download(url, whl, progress)
            with zipfile.ZipFile(whl) as z:
                for info in z.infolist():
                    if any(fnmatch.fnmatch(info.filename, p) for p in patterns):
                        target = staging / "out" / Path(info.filename).name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(info) as src, open(target, "wb") as out:
                            shutil.copyfileobj(src, out)
                        files.append(target.name)
                        total_size += info.file_size
            whl.unlink()
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(staging / "out"), dest)
        # onnxruntime 的插件要和本体放在一起
        ort_files = [f for f in files if f.startswith("onnxruntime_providers_")]
        if ort_files:
            import onnxruntime

            capi = Path(onnxruntime.__file__).parent / "capi"
            for f in ort_files:
                shutil.copy2(dest / f, capi / f)
        save_json(dest / "component.json", {"name": name, "files": files, "size_mb": round(total_size / 2**20),
                                            "installed": int(time.time())})
        env.log(f"组件「{m['label']}」已安装：{len(files)} 个文件，{total_size / 2**30:.2f} GB。重启后生效")
        return status()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def remove(name):
    dest = root() / name
    rec = load_json(dest / "component.json")
    if rec:
        try:
            import onnxruntime

            capi = Path(onnxruntime.__file__).parent / "capi"
            for f in rec["files"]:
                if f.startswith("onnxruntime_providers_") and (capi / f).exists() and getattr(sys, "frozen", False):
                    (capi / f).unlink()  # 开发环境里的插件是 pip 装的，不删
        except Exception:
            pass
    shutil.rmtree(dest, ignore_errors=True)
    return status()
