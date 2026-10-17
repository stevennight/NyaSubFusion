"""可选组件安装（不联网：用本地伪造的 wheel）。"""
import zipfile

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
