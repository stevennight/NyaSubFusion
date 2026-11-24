"""API Key 存取：独立于 config.toml，不把密钥写进配置文件。

后端选择（可用环境变量 NYASUB_KEYRING 覆盖：file / system / memory）：
- Windows / Linux 默认 system：系统凭据库（Windows 凭据管理器 / Secret Service），带进程内缓存避免重复访问。
- macOS 默认 file：用户数据目录下的 secrets.json（权限 0600，仅本人可读）。本地打包的 .app 为临时签名，
  若走 macOS 钥匙串每次重打包都会触发系统反复弹密码窗；需要时仍可用 NYASUB_KEYRING=system 切回钥匙串。
- 测试或无凭据库环境可用 NYASUB_KEYRING=memory（只存在内存里）。

取 Key 的顺序：本模块 → 接口的 api_key_env 环境变量 → 配置里明文写的 api_key（不推荐，兼容旧写法）。
"""
import json
import os
import sys
import threading
from pathlib import Path

from . import env

SERVICE = "NyaSubFusion"
_memory: dict[str, str] = {}
_cache: dict[str, str] = {}
_lock = threading.Lock()


def _mode() -> str:
    m = os.environ.get("NYASUB_KEYRING", "").strip().lower()
    if m in ("memory", "file", "system"):
        return m
    return "file" if sys.platform == "darwin" else "system"


def _file_path() -> Path:
    return env.home() / "secrets.json"


def _read_file() -> dict[str, str]:
    p = _file_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    except Exception:
        pass
    return {}


def _write_file(data: dict[str, str]) -> None:
    p = _file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    if os.name != "nt":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, p)


def _backend():
    if _mode() != "system":
        return None
    try:
        import keyring

        return keyring
    except Exception:
        return None


def get(name: str) -> str:
    mode = _mode()
    if mode == "memory":
        return _memory.get(name, "")
    if mode == "file":
        with _lock:
            return _read_file().get(name, "")
    with _lock:
        if name in _cache:
            return _cache[name]
    kr = _backend()
    if kr is None:
        return _memory.get(name, "")
    try:
        val = kr.get_password(SERVICE, name) or ""
    except Exception:
        val = ""
    with _lock:
        _cache[name] = val
    return val


def set(name: str, key: str) -> None:  # noqa: A001
    mode = _mode()
    if mode == "memory":
        _memory[name] = key
        return
    if mode == "file":
        with _lock:
            data = _read_file()
            data[name] = key
            _write_file(data)
        return
    kr = _backend()
    if kr is None:
        _memory[name] = key
        return
    kr.set_password(SERVICE, name, key)
    with _lock:
        _cache[name] = key


def delete(name: str) -> None:
    mode = _mode()
    if mode == "memory":
        _memory.pop(name, None)
        return
    if mode == "file":
        with _lock:
            data = _read_file()
            if name in data:
                data.pop(name, None)
                _write_file(data)
        return
    kr = _backend()
    if kr is None:
        _memory.pop(name, None)
        return
    try:
        kr.delete_password(SERVICE, name)
    except Exception:
        pass
    with _lock:
        _cache.pop(name, None)


def available() -> str:
    """凭据库的名字，界面上显示用。"""
    mode = _mode()
    if mode == "memory":
        return "内存（测试用，重启后丢失）"
    if mode == "file":
        return "本地受限文件 (secrets.json, 0600)"
    kr = _backend()
    if kr is None:
        return "不可用"
    try:
        return type(kr.get_keyring()).__name__
    except Exception:
        return "不可用"
