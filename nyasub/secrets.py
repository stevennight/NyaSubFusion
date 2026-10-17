"""API Key 存取：放在系统凭据库（Windows 凭据管理器 / macOS 钥匙串 / Secret Service），不写进任何配置文件。

配置里只记接口名；取 Key 的顺序：凭据库 → 接口的 api_key_env 环境变量 → 配置里明文写的 api_key（不推荐，兼容旧写法）。
测试或没有凭据库的环境可以用 NYASUB_KEYRING=memory（只存在内存里）。
"""
import os

SERVICE = "NyaSubFusion"
_memory: dict[str, str] = {}


def _backend():
    if os.environ.get("NYASUB_KEYRING") == "memory":
        return None
    try:
        import keyring

        return keyring
    except Exception:
        return None


def get(name: str) -> str:
    kr = _backend()
    if kr is None:
        return _memory.get(name, "")
    try:
        return kr.get_password(SERVICE, name) or ""
    except Exception:
        return ""


def set(name: str, key: str) -> None:  # noqa: A001
    kr = _backend()
    if kr is None:
        _memory[name] = key
        return
    kr.set_password(SERVICE, name, key)


def delete(name: str) -> None:
    kr = _backend()
    if kr is None:
        _memory.pop(name, None)
        return
    try:
        kr.delete_password(SERVICE, name)
    except Exception:
        pass


def available() -> str:
    """凭据库的名字，界面上显示用。"""
    kr = _backend()
    if kr is None:
        return "内存（测试用，重启后丢失）"
    try:
        return type(kr.get_keyring()).__name__
    except Exception:
        return "不可用"
