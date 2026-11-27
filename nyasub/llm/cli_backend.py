"""本机 CLI 大模型后端：通过常驻 stdio JSON-RPC 协议调用 Antigravity CLI（agy acp）和 Codex CLI（codex app-server）。

特点：
- 复用本机已登录的订阅凭据，无需配置 API Key；
- 常驻子进程按 (kind, exe) 复用，避免每段翻译重复冷启动，但每次请求都新建隔离会话（空目录、只读、禁止调工具）；
- 原生支持图文混排（ACP image base64 / Codex localImage），保证翻译时看得到参考画面；
- 支持 env.check_cancel() 实时取消与 Windows 进程树清理。
"""
import atexit
import base64
import collections
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .. import __version__
from ..env import Cancelled, check_cancel

_NO_TOOLS_GUARD = (
    "你现在作为纯 JSON 文本处理引擎运行。"
    "严禁调用任何工具、严禁读写或检索任何文件、严禁执行命令。"
    "只直接返回用户要求的纯 JSON，不要附加任何解释或额外文字。"
)
_AUDIO_TOOL_GUARD = (
    "你现在作为纯 JSON 音频分析引擎运行。"
    "请先且仅使用文件读取工具打开当前工作目录下的指定音频文件仔细聆听原声，"
    "然后严禁调用其他任何工具、严禁读写其他文件或执行命令，也不要附加任何解释或额外文字，只直接返回用户要求的纯 JSON。"
)
_WEB_RESEARCH_GUARD = (
    "你现在作为只读的影视资料与歌词检索整理引擎运行。"
    "允许使用只读网页搜索工具查询公开百科、官网或歌词站核实信息，严禁读写任何本地文件或执行本地命令。"
    "整理完成后不要附加解释或额外文字，只直接返回用户要求的纯 JSON。"
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_AGY_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
_AGY_EFFORT_RE = re.compile(r"-(low|medium|high|xhigh|max)$")
_POOL_LOCK = threading.Lock()
_POOL: dict[tuple[str, str], "_BaseWorker"] = {}


def _fresh_path() -> str:
    """合并当前进程 PATH 和系统常见安装目录（Windows 注册表 / macOS Homebrew 等，装完 CLI 不用重启软件也能找到）。"""
    cur = os.environ.get("PATH", "")
    if sys.platform == "darwin":
        home = Path.home()
        cur_parts = [p for p in cur.split(os.pathsep) if p]
        extra = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            str(home / ".local" / "bin"),
            str(home / ".cargo" / "bin"),
            str(home / ".gemini" / "antigravity" / "bin"),
            str(home / "Library" / "Application Support" / "Antigravity" / "bin"),
        ]
        return os.pathsep.join(cur_parts + [p for p in extra if p not in cur_parts])
    if sys.platform != "win32":
        return cur
    parts = [cur]
    try:
        import winreg

        for root, sub in (
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ):
            try:
                with winreg.OpenKey(root, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    if val:
                        parts.append(os.path.expandvars(val))
            except OSError:
                pass
    except ImportError:
        pass
    return os.pathsep.join(p for p in parts if p)


def _find_windows_codex_appx() -> str | None:
    """探测 Windows 商店版 Codex 桌面端自带的 codex.exe（挑最新版本）。"""
    if sys.platform != "win32":
        return None
    base = Path(r"C:\Program Files\WindowsApps")
    try:
        cands = sorted(base.glob(r"OpenAI.Codex_*\app\resources\codex.exe"), reverse=True)
        for p in cands:
            if p.is_file():
                return str(p)
    except OSError:
        pass
    # WindowsApps 目录有时不可直接列目录，退回用 Get-AppxPackage 查安装路径
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-AppxPackage OpenAI.Codex).InstallLocation"],
            capture_output=True, text=True, timeout=6, creationflags=_NO_WINDOW,
        )
        for line in sorted((r.stdout or "").splitlines(), reverse=True):
            loc = line.strip()
            if loc:
                exe = Path(loc) / "app" / "resources" / "codex.exe"
                if exe.is_file():
                    return str(exe)
    except Exception:
        pass
    return None


def find_executable(kind: str, configured: str = "") -> str | None:
    """查找本机 agy / codex 可执行文件；找不到返回 None。"""
    configured = (configured or "").strip()
    path_env = _fresh_path()
    if configured:
        exp = os.path.expanduser(os.path.expandvars(configured))
        if Path(exp).is_file():
            return str(Path(exp).resolve())
        return shutil.which(exp, path=path_env)

    names = ("agy", "agy.exe", "agy.cmd") if kind == "agy" else ("codex", "codex.exe", "codex.cmd")
    for n in names:
        hit = shutil.which(n, path=path_env)
        if hit:
            return hit

    home = Path.home()
    local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    roaming_app = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    if kind == "agy":
        candidates = [
            local_app / "agy" / "bin" / "agy.exe",
            home / ".agy" / "bin" / "agy.exe",
            home / ".local" / "bin" / ("agy.exe" if sys.platform == "win32" else "agy"),
            home / ".gemini" / "antigravity" / "bin" / "agy",
            home / "Library" / "Application Support" / "Antigravity" / "bin" / "agy",
            Path("/opt/homebrew/bin/agy"),
            Path("/usr/local/bin/agy"),
        ]
    elif kind == "codex":
        candidates = [
            roaming_app / "npm" / "codex.cmd",
            home / ".cargo" / "bin" / ("codex.exe" if sys.platform == "win32" else "codex"),
            local_app / "hermes" / "node" / "codex.cmd",
            home / ".local" / "bin" / ("codex.exe" if sys.platform == "win32" else "codex"),
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
            Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
            home / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex-cli" / "bin" / "codex",
            Path("/Applications/Codex.app/Contents/Resources/codex"),
            home / "Applications" / "Codex.app" / "Contents" / "Resources" / "codex",
        ]
    else:
        return None
    for p in candidates:
        if p.is_file():
            return str(p)
    if kind == "codex":
        return _find_windows_codex_appx()
    return None


def _cmd_args(exe: str, *args: str) -> list[str]:
    if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *args]
    return [exe, *args]


def _kill_proc(proc: subprocess.Popen | None):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True, timeout=5, creationflags=_NO_WINDOW,
                )
            else:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    for s in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if s:
                s.close()
        except Exception:
            pass


def _mime(path: str) -> str:
    suf = Path(path).suffix.lower()
    if suf == ".png":
        return "image/png"
    if suf == ".webp":
        return "image/webp"
    return "image/jpeg"


def _fatal(msg: str, status: int | None = None):
    from .client import FatalLLMError

    err = FatalLLMError(msg)
    err.status = status
    return err


def _is_fatal_message(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in (
        "unauthenticated", "unauthorized", "not logged in", "login required", "auth",
        "model not found", "unknown model", "invalid model", "unsupported model",
        "permission denied", "forbidden",
    ))


class _BaseWorker:
    kind: str = ""

    def __init__(self, exe: str):
        self.exe = exe
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self._cwd: str | None = None
        self._q: queue.Queue = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=100)
        self._rid = 0

    def _spawn_cmd(self) -> list[str]:
        raise NotImplementedError

    def _handshake(self):
        raise NotImplementedError

    def _ensure_started(self):
        if self.proc is not None and self.proc.poll() is None and self._cwd and Path(self._cwd).is_dir():
            return
        self.close()
        self._cwd = tempfile.mkdtemp(prefix=f"nyasub-{self.kind}-")
        self._q = queue.Queue()
        self._stderr.clear()
        self._rid = 0
        self.proc = subprocess.Popen(
            self._spawn_cmd(),
            cwd=self._cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_NO_WINDOW,
        )
        threading.Thread(target=self._read_stdout, args=(self.proc, self._q), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self.proc, self._stderr), daemon=True).start()
        try:
            self._handshake()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _read_stdout(proc: subprocess.Popen, q: queue.Queue):
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    q.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        finally:
            q.put(None)

    @staticmethod
    def _read_stderr(proc: subprocess.Popen, buf: collections.deque):
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                s = line.strip()
                if s:
                    buf.append(s)
        except Exception:
            pass

    def _stderr_tail(self) -> str:
        return " | ".join(list(self._stderr)[-6:])[:400]

    def _send(self, obj: dict):
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            raise RuntimeError(f"{self.kind} 常驻进程已退出（{self._stderr_tail()}）")
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _next_msg(self, deadline: float, on_cancel=None) -> dict:
        while True:
            try:
                check_cancel()
            except Cancelled:
                if on_cancel:
                    try:
                        on_cancel()
                    except Exception:
                        pass
                self.close()
                raise
            rem = deadline - time.time()
            if rem <= 0:
                self.close()
                raise TimeoutError(f"{self.kind} 响应超时")
            try:
                msg = self._q.get(timeout=min(0.25, rem))
            except queue.Empty:
                continue
            if msg is None:
                tail = self._stderr_tail()
                self.close()
                raise RuntimeError(f"{self.kind} 进程意外退出：{tail or 'EOF'}")
            return msg

    def _rpc(self, method: str, params: dict, timeout: float = 30.0, on_msg=None, on_cancel=None) -> dict:
        self._rid += 1
        rid = self._rid
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            msg = self._next_msg(deadline, on_cancel=on_cancel)
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                return msg
            if on_msg:
                on_msg(msg)

    def close(self):
        proc, self.proc = self.proc, None
        _kill_proc(proc)
        cwd, self._cwd = self._cwd, None
        if cwd:
            shutil.rmtree(cwd, ignore_errors=True)


class AgyAcpWorker(_BaseWorker):
    kind = "agy"

    def _spawn_cmd(self) -> list[str]:
        return _cmd_args(self.exe, "acp", "--disable-telemetry")

    def _handshake(self):
        res = self._rpc(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "nyasub", "version": __version__},
            },
            timeout=30.0,
        )
        if "error" in res:
            raise RuntimeError(f"agy acp initialize 失败：{res['error']}")

    @staticmethod
    def _resolve_model(model: str, effort: str, available: list[dict]) -> str:
        avail_ids = {m.get("modelId") for m in available if isinstance(m, dict) and m.get("modelId")}
        effort = (effort or "").strip().lower()
        if effort and avail_ids:
            base = re.sub(r"-(?:low|medium|high|xhigh|max)$", "", model)
            cand = f"{base}-{effort}"
            if cand in avail_ids:
                return cand
        if model in avail_ids or not avail_ids:
            return model
        # 允许填不带档位后缀的基础模型名（例如 gemini-4-neon → 优先选 medium，其次第一个匹配）
        pref = f"{model}-"
        for suffix in ("medium", "high", "low"):
            if f"{pref}{suffix}" in avail_ids:
                return f"{pref}{suffix}"
        for mid in sorted(avail_ids):
            if mid.startswith(pref):
                return mid
        return model

    def list_models(self) -> list[dict]:
        with self.lock:
            self._ensure_started()
            assert self._cwd is not None
            new_res = self._rpc("session/new", {"cwd": self._cwd, "mcpServers": []}, timeout=30.0)
            if "error" in new_res:
                raise RuntimeError(f"agy session/new 失败：{new_res['error']}")
            r0 = new_res.get("result") or {}
            sid = r0.get("sessionId")
            if sid:
                try:
                    self._rpc("session/close", {"sessionId": sid}, timeout=5.0)
                except Exception:
                    pass
            cur = (r0.get("models") or {}).get("currentModelId") or ""
            avail = [m for m in ((r0.get("models") or {}).get("availableModels") or []) if isinstance(m, dict) and m.get("modelId")]
            avail_ids = {m["modelId"] for m in avail}
            base_efforts: dict[str, list[str]] = {}
            for mid in avail_ids:
                hit = _AGY_EFFORT_RE.search(mid)
                if hit:
                    base = _AGY_EFFORT_RE.sub("", mid)
                    base_efforts.setdefault(base, []).append(hit.group(1))
            rank = {e: idx for idx, e in enumerate(_AGY_EFFORT_ORDER)}
            out = []
            for m in avail:
                mid = m["modelId"]
                base = _AGY_EFFORT_RE.sub("", mid)
                effs = sorted(set(base_efforts.get(base, [])), key=lambda x: (rank.get(x, 99), x))
                item = {"id": mid, "name": m.get("name") or mid, "default": mid == cur}
                if effs:
                    item["efforts"] = effs
                out.append(item)
            return out

    def call(self, model: str, system: str, user: str, images=None, audio=None, effort: str = "", timeout: float = 180.0, allow_web: bool = False):
        with self.lock:
            self._ensure_started()
            assert self._cwd is not None
            staged_name = ""
            staged_path: Path | None = None
            if audio:
                audio_path, fmt = audio
                ext = (fmt or Path(audio_path).suffix.lstrip(".") or "mp3").lower().lstrip(".")
                staged_name = f"audio_{self._rid + 1}.{ext}"
                staged_path = Path(self._cwd) / staged_name
                shutil.copy2(audio_path, staged_path)
            guard = _AUDIO_TOOL_GUARD if staged_name else (_WEB_RESEARCH_GUARD if allow_web else _NO_TOOLS_GUARD)
            try:
                new_res = self._rpc(
                    "session/new",
                    {
                        "cwd": self._cwd,
                        "mcpServers": [],
                        "_meta": {
                            "antigravity": {
                                "systemPrompt": {
                                    "append": (guard + ("\n\n" + system if system else "")),
                                }
                            }
                        },
                    },
                    timeout=30.0,
                )
                if "error" in new_res:
                    msg = str(new_res["error"])
                    if _is_fatal_message(msg):
                        raise _fatal(f"Antigravity CLI 错误（请先在终端运行 agy 完成登录）：{msg[:300]}", status=401)
                    raise RuntimeError(f"agy session/new 失败：{msg[:300]}")

                r0 = new_res.get("result") or {}
                sid = r0.get("sessionId")
                if not sid:
                    raise RuntimeError(f"agy session/new 未返回 sessionId：{str(r0)[:200]}")

                avail = (r0.get("models") or {}).get("availableModels") or []
                target = self._resolve_model(model, effort, avail)
                if target:
                    set_res = self._rpc("session/set_model", {"sessionId": sid, "modelId": target}, timeout=20.0)
                    if "error" in set_res:
                        err = str(set_res["error"])
                        raise _fatal(f"Antigravity CLI 模型「{target}」不可用：{err[:300]}", status=404)

                prompt_text = (
                    f"【音频文件】请先用文件读取工具打开当前目录下的 {staged_name} 仔细听完原声（禁止跳过或凭空猜测），再按要求输出纯 JSON。\n\n{user}"
                    if staged_name
                    else user
                )
                blocks: list[dict] = [{"type": "text", "text": prompt_text}]
                for img in images or []:
                    img = img if isinstance(img, dict) else {"path": img}
                    if img.get("label"):
                        blocks.append({"type": "text", "text": img["label"]})
                    b64 = base64.b64encode(Path(img["path"]).read_bytes()).decode()
                    blocks.append({"type": "image", "mimeType": _mime(img["path"]), "data": b64})

                chunks: list[str] = []
                audio_read = False
                audio_read_failed = False
                audio_tool_ids: set[str] = set()

                def on_msg(msg: dict):
                    nonlocal audio_read, audio_read_failed
                    if "id" in msg and "method" in msg:
                        params = msg.get("params") or {}
                        tc = params.get("toolCall") or {}
                        allow_audio = staged_name and tc.get("kind") == "read" and staged_name.lower() in json.dumps(params, ensure_ascii=False).lower()
                        tc_dump = json.dumps(tc, ensure_ascii=False).lower()
                        allow_search = allow_web and (tc.get("kind") in ("search", "fetch") or any(k in tc_dump for k in ("search_web", "web_search", "fetch_url", "read_url")))
                        if allow_audio or allow_search:
                            opts = params.get("options") or []
                            opt = next(
                                (o.get("optionId") for o in opts if isinstance(o, dict) and o.get("kind") == "allow_once" and o.get("optionId")),
                                None,
                            ) or next(
                                (o.get("optionId") for o in opts if isinstance(o, dict) and str(o.get("kind", "")).startswith("allow") and o.get("optionId")),
                                None,
                            )
                            if opt:
                                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": {"outcome": "selected", "optionId": opt}}})
                                return
                        # 严禁任何非预期工具调用：一律取消权限请求，避免挂起
                        self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                        return
                    if msg.get("method") == "session/update":
                        upd = (msg.get("params") or {}).get("update") or {}
                        ut = upd.get("sessionUpdate")
                        if ut in ("tool_call", "tool_call_update") and staged_name:
                            tid = str(upd.get("toolCallId") or "")
                            matched = staged_name.lower() in json.dumps(upd, ensure_ascii=False).lower()
                            if matched:
                                audio_read = True
                                if tid:
                                    audio_tool_ids.add(tid)
                            if matched or (tid and tid in audio_tool_ids):
                                st = upd.get("status")
                                if st == "failed":
                                    audio_read_failed = True
                                elif st == "completed":
                                    audio_read_failed = False
                        elif ut == "agent_message_chunk":
                            c = upd.get("content") or {}
                            if c.get("type") == "text":
                                chunks.append(c.get("text", ""))

                def on_cancel():
                    self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})

                res = self._rpc(
                    "session/prompt",
                    {"sessionId": sid, "prompt": blocks},
                    timeout=max(30.0, float(timeout)),
                    on_msg=on_msg,
                    on_cancel=on_cancel,
                )
                try:
                    self._rpc("session/close", {"sessionId": sid}, timeout=5.0)
                except Exception:
                    pass
            finally:
                if staged_path is not None:
                    staged_path.unlink(missing_ok=True)

            if "error" in res:
                err = res["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                if _is_fatal_message(msg):
                    raise _fatal(f"Antigravity CLI 报错：{msg[:300]}", status=401)
                raise RuntimeError(f"Antigravity CLI 请求失败：{msg[:300]}")

            if staged_name:
                if audio_read_failed:
                    raise _fatal(f"Antigravity CLI 模型「{target}」无法读取音频文件（可能不支持音频，请改用 Gemini 系列模型）", status=415)
                if not audio_read:
                    raise RuntimeError(f"Antigravity CLI（{target}）未读取音频文件 {staged_name}，可能该模型不支持音频输入")

            body = res.get("result") or {}
            raw_u = body.get("usage") or {}
            usage = None
            if raw_u:
                usage = {
                    "prompt_tokens": raw_u.get("inputTokens"),
                    "completion_tokens": raw_u.get("outputTokens"),
                    "total_tokens": raw_u.get("totalTokens"),
                    "cached_tokens": raw_u.get("cachedReadTokens"),
                    "reasoning_tokens": raw_u.get("thoughtTokens"),
                }
            return "".join(chunks), usage


class CodexAppServerWorker(_BaseWorker):
    kind = "codex"

    def _spawn_cmd(self) -> list[str]:
        return _cmd_args(self.exe, "app-server", "--stdio")

    def _handshake(self):
        res = self._rpc(
            "initialize",
            {"clientInfo": {"name": "nyasub", "version": __version__}},
            timeout=30.0,
        )
        if "error" in res:
            raise RuntimeError(f"codex app-server initialize 失败：{res['error']}")
        self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def list_models(self) -> list[dict]:
        with self.lock:
            self._ensure_started()
            res = self._rpc("model/list", {}, timeout=20.0)
            if "error" in res:
                raise RuntimeError(f"codex model/list 失败：{res['error']}")
            data = (res.get("result") or {}).get("data") or []
            out = []
            for m in data:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                efforts = [e.get("reasoningEffort") for e in (m.get("supportedReasoningEfforts") or []) if isinstance(e, dict) and e.get("reasoningEffort")]
                out.append({
                    "id": m["id"],
                    "name": m.get("displayName") or m["id"],
                    "efforts": efforts,
                    "default": bool(m.get("isDefault")),
                })
            return out

    def call(self, model: str, system: str, user: str, images=None, audio=None, effort: str = "", timeout: float = 180.0, allow_web: bool = False):
        if audio:
            raise _fatal("Codex CLI 不支持音频输入，请选择 OpenAI 兼容接口或 Antigravity CLI", status=415)
        with self.lock:
            self._ensure_started()
            assert self._cwd is not None
            guard = _WEB_RESEARCH_GUARD if allow_web else _NO_TOOLS_GUARD
            th = self._rpc(
                "thread/start",
                {
                    "cwd": self._cwd,
                    "model": model,
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "baseInstructions": guard + ("\n\n" + system if system else ""),
                },
                timeout=30.0,
            )
            if "error" in th:
                msg = str(th["error"])
                if _is_fatal_message(msg):
                    raise _fatal(f"Codex CLI 错误（请确认已登录 Codex）：{msg[:300]}", status=401)
                raise RuntimeError(f"codex thread/start 失败：{msg[:300]}")
            tid = ((th.get("result") or {}).get("thread") or {}).get("id")
            if not tid:
                raise RuntimeError(f"codex thread/start 未返回 thread.id：{str(th)[:200]}")

            items: list[dict] = [{"type": "text", "text": user}]
            for img in images or []:
                img = img if isinstance(img, dict) else {"path": img}
                if img.get("label"):
                    items.append({"type": "text", "text": img["label"]})
                items.append({
                    "type": "localImage",
                    "path": str(Path(img["path"]).resolve()),
                    "detail": img.get("detail") or "low",
                })

            turn_params = {
                "threadId": tid,
                "model": model,
                "input": items,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly"},
            }
            if (effort or "").strip():
                turn_params["effort"] = effort.strip()

            turn_res = self._rpc("turn/start", turn_params, timeout=30.0)
            if "error" in turn_res:
                msg = str(turn_res["error"])
                if _is_fatal_message(msg):
                    raise _fatal(f"Codex CLI 请求参数/模型错误：{msg[:300]}", status=400)
                raise RuntimeError(f"codex turn/start 失败：{msg[:300]}")
            turn_id = ((turn_res.get("result") or {}).get("turn") or {}).get("id")

            deltas: list[str] = []
            final_text = ""
            usage = None
            turn_obj = None
            deadline = time.time() + max(30.0, float(timeout))

            def on_cancel():
                if turn_id:
                    self._send({"jsonrpc": "2.0", "method": "turn/interrupt", "params": {"threadId": tid, "turnId": turn_id}})

            while True:
                msg = self._next_msg(deadline, on_cancel=on_cancel)
                if "id" in msg and "method" in msg:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"decision": "decline"}})
                    continue
                m = msg.get("method")
                params = msg.get("params") or {}
                if m == "item/agentMessage/delta":
                    deltas.append(params.get("delta", ""))
                elif m == "item/completed":
                    it = params.get("item") or {}
                    if it.get("type") == "agentMessage" and it.get("text"):
                        final_text = it["text"]
                elif m == "thread/tokenUsage/updated":
                    tot = (params.get("tokenUsage") or {}).get("last") or (params.get("tokenUsage") or {}).get("total")
                    if isinstance(tot, dict):
                        usage = {
                            "prompt_tokens": tot.get("inputTokens"),
                            "completion_tokens": tot.get("outputTokens"),
                            "total_tokens": tot.get("totalTokens"),
                            "cached_tokens": tot.get("cachedInputTokens"),
                            "reasoning_tokens": tot.get("reasoningOutputTokens"),
                        }
                elif m == "turn/completed":
                    turn_obj = params.get("turn") or params
                    break

            err = (turn_obj or {}).get("error")
            status = (turn_obj or {}).get("status")
            if err or status == "failed":
                info = err.get("codexErrorInfo") if isinstance(err, dict) else None
                msg = (err.get("message") if isinstance(err, dict) else str(err)) or f"status={status}"
                if info in ("unauthorized", "modelNotFound", "badRequest", "cyberPolicy", "misalignmentPolicyViolation") or _is_fatal_message(msg):
                    code = 401 if info == "unauthorized" else (404 if info == "modelNotFound" else 400)
                    raise _fatal(f"Codex CLI 报错（{info or code}）：{msg[:300]}", status=code)
                if info in ("usageLimitExceeded", "rateLimitExceeded"):
                    raise RuntimeError(f"Codex 订阅额度已达上限或限流（{info}）：{msg[:240]}")
                raise RuntimeError(f"Codex CLI 请求失败（{info or status}）：{msg[:300]}")

            return final_text or "".join(deltas), usage


def _get_worker(kind: str, configured: str = "") -> _BaseWorker:
    exe = find_executable(kind, configured)
    if not exe:
        hint = "请先安装 Antigravity CLI（irm https://antigravity.google/cli/install.ps1 | iex）或在设置里填写 agy 路径" if kind == "agy" \
            else "请先安装 Codex CLI / Codex 桌面端，或在设置里填写 codex.exe 路径"
        raise _fatal(f"找不到本机 {kind} 程序：{hint}", status=404)
    key = (kind, exe)
    with _POOL_LOCK:
        w = _POOL.get(key)
        if w is None:
            w = AgyAcpWorker(exe) if kind == "agy" else CodexAppServerWorker(exe)
            _POOL[key] = w
        return w


def call_cli(kind: str, command: str, model: str, system: str, user: str, images=None, audio=None, effort: str = "", timeout: float = 180.0, allow_web: bool = False):
    """通过常驻 CLI 进程发起一次请求，返回 (回复文本, usage dict 或 None)。"""
    w = _get_worker(kind, command)
    return w.call(model=model, system=system, user=user, images=images, audio=audio, effort=effort, timeout=timeout, allow_web=allow_web)


def detect_cli(kind: str, configured: str = "", check_version: bool = True) -> dict:
    """探测本机 CLI 是否安装及版本（不报错）。check_version=False 时只做快速路径检查。"""
    exe = find_executable(kind, configured)
    if not exe:
        return {"installed": False, "path": "", "version": "", "error": f"未找到 {kind}"}
    if not check_version:
        return {"installed": True, "path": exe, "version": "", "error": ""}
    ver = ""
    try:
        r = subprocess.run(_cmd_args(exe, "--version"), capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW)
        ver = ((r.stdout or "") + "\n" + (r.stderr or "")).strip().splitlines()[0].strip() if (r.stdout or r.stderr) else ""
    except Exception as e:
        return {"installed": True, "path": exe, "version": "", "error": str(e)[:120]}
    return {"installed": True, "path": exe, "version": ver, "error": ""}


def list_cli_models(kind: str, configured: str = "") -> dict:
    """列出本机 CLI 当前账号可用的模型列表（通过常驻协议获取，同时预热连接）。"""
    info = detect_cli(kind, configured)
    if not info["installed"]:
        return {"ok": False, **info, "models": []}
    exe = info["path"]
    try:
        if kind == "agy":
            w = _get_worker("agy", exe)
            assert isinstance(w, AgyAcpWorker)
            return {"ok": True, **info, "models": w.list_models()}
        if kind == "codex":
            w = _get_worker("codex", exe)
            assert isinstance(w, CodexAppServerWorker)
            return {"ok": True, **info, "models": w.list_models()}
        return {"ok": False, **info, "error": f"不支持的 CLI 类型：{kind}", "models": []}
    except Exception as e:
        return {"ok": False, **info, "error": str(e)[:300], "models": []}


def test_cli_provider(kind: str, model: str, command: str = "") -> dict:
    """设置页“测试连接”：走常驻协议发极简问候，验证登录与模型可用。"""
    t0 = time.time()
    if not (model or "").strip():
        return {"ok": False, "error": "缺少模型名"}
    try:
        reply, _ = call_cli(
            kind=kind,
            command=command,
            model=model.strip(),
            system='只输出 JSON：{"reply": "好"}',
            user='回复“好”一个字，输出 JSON：{"reply": "好"}',
            timeout=60.0,
        )
        from .client import parse_json

        shown = reply.strip()
        try:
            d = parse_json(shown)
            if isinstance(d, dict) and d.get("reply"):
                shown = str(d["reply"])
        except Exception:
            pass
        return {"ok": True, "ms": int((time.time() - t0) * 1000), "reply": shown[:40]}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "error": str(e)[:300]}


def close_all():
    """关闭所有常驻 CLI 进程（软件退出 / 测试结束时调用）。"""
    with _POOL_LOCK:
        workers = list(_POOL.values())
        _POOL.clear()
    for w in workers:
        try:
            w.close()
        except Exception:
            pass


atexit.register(close_all)
