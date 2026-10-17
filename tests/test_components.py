"""可选组件安装（不联网：用本地伪造的 wheel）。"""
import zipfile
from pathlib import Path

from nyasub import components


def test_install_extracts_only_dlls(tmp_path, monkeypatch, isolated_home):
    whl = tmp_path / "nvidia_cublas_cu12-12.9.0-py3-none-win_amd64.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("nvidia/cublas/bin/cublas64_12.dll", b"x" * 10)
        z.writestr("nvidia/cublas/include/cublas.h", b"header")
        z.writestr("nvidia_cublas_cu12-12.9.0.dist-info/METADATA", b"meta")

    monkeypatch.setattr(components, "manifest", lambda: {"t": {"label": "测试", "detail": "", "packages": [("nvidia-cublas-cu12", "")]}})
    monkeypatch.setattr(components, "_pick_wheel", lambda idx, pkg, spec: ("12.9.0", f"file://{whl}"))
    monkeypatch.setattr(components, "_download", lambda url, dest, progress: (dest.write_bytes(whl.read_bytes()), progress(1, 1)))

    st = components.install("t", index="https://example.invalid/simple")
    comp = isolated_home / "components" / "t"
    assert (comp / "cublas64_12.dll").exists() and not (comp / "cublas.h").exists()
    assert st["components"][0]["installed"] and st["components"][0]["files"] == 1
    components.remove("t")
    assert not comp.exists()


def _ort_setup(tmp_path, monkeypatch, shared_bytes):
    """伪造 onnxruntime-gpu 的 wheel 和一个“正在运行”的 onnxruntime：capi 里的 providers_shared.dll 被锁住，不能覆盖。"""
    import sys
    import types

    whl = tmp_path / "onnxruntime_gpu-1.0.0-cp311-cp311-win_amd64.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("onnxruntime/capi/onnxruntime_providers_cuda.dll", b"cuda" * 10)
        z.writestr("onnxruntime/capi/onnxruntime_providers_shared.dll", b"shared")
    capi = tmp_path / "ort" / "capi"
    capi.mkdir(parents=True)
    (capi / "onnxruntime_providers_shared.dll").write_bytes(shared_bytes)
    fake = types.ModuleType("onnxruntime")
    fake.__file__ = str(tmp_path / "ort" / "__init__.py")
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    pats = ["onnxruntime/capi/onnxruntime_providers_cuda.dll", "onnxruntime/capi/onnxruntime_providers_shared.dll"]
    monkeypatch.setattr(components, "manifest", lambda: {"o": {"label": "测试", "detail": "", "packages": [("onnxruntime-gpu", "", pats)]}})
    monkeypatch.setattr(components, "_pick_wheel", lambda idx, pkg, spec: ("1.0.0", "x"))
    monkeypatch.setattr(components, "_download", lambda url, dest, progress: dest.write_bytes(whl.read_bytes()))
    real_replace, real_copy = components.os.replace, components.shutil.copy2

    def locked(fn):
        def wrap(src, dst, *a, **k):
            if Path(dst).parent.name == "capi" and Path(dst).name == "onnxruntime_providers_shared.dll":
                raise PermissionError(32, "in use")
            return fn(src, dst, *a, **k)
        return wrap
    monkeypatch.setattr(components.os, "replace", locked(real_replace))
    monkeypatch.setattr(components.shutil, "copy2", locked(real_copy))
    return capi


def test_install_skips_identical_file_in_use(tmp_path, monkeypatch, isolated_home):
    capi = _ort_setup(tmp_path, monkeypatch, b"shared")
    # 上次装到一半：组件目录里已经有一个同样的文件
    comp = isolated_home / "components" / "o"
    comp.mkdir(parents=True)
    (comp / "onnxruntime_providers_shared.dll").write_bytes(b"shared")
    (comp / "stale.dll").write_bytes(b"old")
    st = components.install("o", index="x")
    assert st["components"][0]["installed"]
    assert (capi / "onnxruntime_providers_cuda.dll").read_bytes() == b"cuda" * 10
    assert not (comp / "stale.dll").exists()


def test_install_reports_file_in_use(tmp_path, monkeypatch, isolated_home):
    import pytest

    _ort_setup(tmp_path, monkeypatch, b"different")
    with pytest.raises(RuntimeError, match="重启程序后再安装"):
        components.install("o", index="x")
    assert not components.status()["components"][0]["installed"]
