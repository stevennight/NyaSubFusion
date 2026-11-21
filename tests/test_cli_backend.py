"""本机 CLI 大模型后端（agy acp / codex app-server）与按用途拆分（翻译 / 整理 / 视觉复核）测试。"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from nyasub import env, secrets
from nyasub.core import config
from nyasub.llm import cli_backend, listen
from nyasub.llm.client import FatalLLMError, LLM
from nyasub.sources import asr_api

MOCK_ACP_SCRIPT = r"""
import json, sys, time
from pathlib import Path

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
turn_count = 0
last_sys = ""
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    m = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if m == "initialize":
        out = {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": True, "audio": False}},
            "agentInfo": {"name": "mock-acp", "version": "1.0"},
        }}
    elif m == "session/new":
        last_sys = (((params.get("_meta") or {}).get("antigravity") or {}).get("systemPrompt") or {}).get("append", "")
        out = {"jsonrpc": "2.0", "id": rid, "result": {
            "sessionId": "s-1",
            "models": {"availableModels": [
                {"modelId": "gemini-4-neon-low", "name": "Neon Low"},
                {"modelId": "gemini-4-neon-medium", "name": "Neon Med"},
                {"modelId": "gemini-4-neon-high", "name": "Neon High"},
            ]},
        }}
        sys.stderr.write("SYS_APPEND=" + last_sys[:60] + "\n")
        sys.stderr.flush()
    elif m == "session/set_model":
        selected_model = params.get("modelId")
        if selected_model == "no-such-model":
            out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown model"}}
        else:
            out = {"jsonrpc": "2.0", "id": rid, "result": {"modelId": selected_model}}
    elif m == "session/prompt":
        turn_count += 1
        if mode == "slow":
            time.sleep(5)
        blocks = params.get("prompt") or []
        imgs = [b for b in blocks if b.get("type") == "image"]
        if mode in ("audio_ok", "audio_fail", "audio_unread"):
            afiles = sorted(Path(".").glob("audio_*.*"))
            if mode == "audio_ok":
                if not afiles:
                    raise RuntimeError("missing staged audio file")
                aname = afiles[0].name
                sys.stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": 997, "method": "session/request_permission",
                    "params": {
                        "toolCall": {"kind": "read", "title": f"Read {aname}"},
                        "options": [{"optionId": "allow-one", "kind": "allow_once"}, {"optionId": "deny", "kind": "reject_once"}],
                    },
                }) + "\n")
                sys.stdout.flush()
                perm_read = json.loads(sys.stdin.readline())
                assert (perm_read.get("result") or {}).get("outcome") == {"outcome": "selected", "optionId": "allow-one"}
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "s-1",
                    "update": {"sessionUpdate": "tool_call", "toolCallId": "tc-aud-1", "title": f"File {aname}", "status": "in_progress"},
                }}) + "\n")
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "s-1",
                    "update": {"sessionUpdate": "tool_call_update", "toolCallId": "tc-aud-1", "status": "completed"},
                }}) + "\n")
                sys.stdout.flush()
            elif mode == "audio_fail":
                aname = afiles[0].name if afiles else "audio_missing.mp3"
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "s-1",
                    "update": {"sessionUpdate": "tool_call", "toolCallId": "tc-aud-fail", "title": f"File {aname}", "status": "in_progress"},
                }}) + "\n")
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "s-1",
                    "update": {"sessionUpdate": "tool_call_update", "toolCallId": "tc-aud-fail", "status": "failed"},
                }}) + "\n")
                sys.stdout.flush()
            # 同样确认非授权命令工具依然会被拒绝
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 999, "method": "session/request_permission", "params": {"tool": "run_command"}}) + "\n")
            sys.stdout.flush()
            perm = json.loads(sys.stdin.readline())
            assert (perm.get("result") or {}).get("outcome", {}).get("outcome") == "cancelled"
            prompt_text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            if '"segments"' in last_sys or "逐字转写员" in last_sys or "这段音频长" in prompt_text:
                payload = json.dumps({"language": "ja", "segments": [{"start": "00:00.10", "end": "00:00.80", "text": "風の中に"}]}, ensure_ascii=False)
            else:
                payload = json.dumps({"notes": [{"id": "q1", "end": "上扬", "kind": "问句", "tone": "轻声", "heard": ""}]}, ensure_ascii=False)
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "s-1",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": payload}},
            }}) + "\n")
            sys.stdout.flush()
            out = {"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn", "usage": {"inputTokens": 900, "outputTokens": 40, "totalTokens": 940}}}
        else:
            # 故意先发一次工具权限请求，验证客户端会自动拒绝而不挂起
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 999, "method": "session/request_permission", "params": {"tool": "run_command"}}) + "\n")
            sys.stdout.flush()
            perm = json.loads(sys.stdin.readline())
            if mode == "flaky" and turn_count == 1:
                out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "rate limit exceeded"}}
            else:
                payload = json.dumps({
                    "ok": True,
                    "model": selected_model,
                    "images": len(imgs),
                    "mime": imgs[0]["mimeType"] if imgs else "",
                    "denied": (perm.get("result") or {}).get("outcome", {}).get("outcome") == "cancelled",
                }, ensure_ascii=False)
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "s-1",
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": payload}},
                }}) + "\n")
                sys.stdout.flush()
                out = {"jsonrpc": "2.0", "id": rid, "result": {
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 1200, "outputTokens": 80, "totalTokens": 1280, "cachedReadTokens": 400, "thoughtTokens": 20},
                }}
    elif m == "session/close":
        out = {"jsonrpc": "2.0", "id": rid, "result": {}}
    else:
        continue
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
"""

MOCK_CODEX_SCRIPT = r"""
import json, sys, time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
turn_count = 0
last_base = ""
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    m = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if m == "initialize":
        out = {"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "mock-codex/1.0"}}
    elif m == "initialized":
        continue
    elif m == "model/list":
        out = {"jsonrpc": "2.0", "id": rid, "result": {"data": [
            {"id": "gpt-6.2-sol", "displayName": "GPT-6.2-Sol", "isDefault": True,
             "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]},
        ]}}
    elif m == "thread/start":
        last_base = params.get("baseInstructions") or ""
        out = {"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": "th-1"}}}
    elif m == "turn/start":
        turn_count += 1
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": "tu-1", "status": "inProgress"}}}) + "\n")
        sys.stdout.flush()
        if mode == "slow":
            time.sleep(5)
        # 故意先发一次审批请求，验证客户端会自动 decline
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 888, "method": "item/commandExecution/requestApproval", "params": {}}) + "\n")
        sys.stdout.flush()
        appr = json.loads(sys.stdin.readline())
        if mode == "unauth":
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "turn/completed", "params": {
                "turn": {"id": "tu-1", "status": "failed", "error": {"codexErrorInfo": "unauthorized", "message": "not signed in"}},
            }}) + "\n")
            sys.stdout.flush()
            continue
        if mode == "flaky" and turn_count == 1:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "turn/completed", "params": {
                "turn": {"id": "tu-1", "status": "failed", "error": {"codexErrorInfo": "rateLimitExceeded", "message": "slow down"}},
            }}) + "\n")
            sys.stdout.flush()
            continue
        inputs = params.get("input") or []
        imgs = [x for x in inputs if x.get("type") == "localImage"]
        payload = json.dumps({
            "ok": True,
            "model": params.get("model"),
            "effort": params.get("effort") or "",
            "images": len(imgs),
            "detail": imgs[0]["detail"] if imgs else "",
            "has_base": "SYS_TAG" in last_base,
            "declined": (appr.get("result") or {}).get("decision") == "decline",
        }, ensure_ascii=False)
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "thread/tokenUsage/updated", "params": {
            "tokenUsage": {"last": {"inputTokens": 900, "outputTokens": 60, "totalTokens": 960, "cachedInputTokens": 200, "reasoningOutputTokens": 15}},
        }}) + "\n")
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "item/completed", "params": {
            "item": {"type": "agentMessage", "text": payload},
        }}) + "\n")
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "turn/completed", "params": {
            "turn": {"id": "tu-1", "status": "completed"},
        }}) + "\n")
        sys.stdout.flush()
        continue
    else:
        continue
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
"""


@pytest.fixture(autouse=True)
def _cleanup_workers():
    cli_backend.close_all()
    yield
    cli_backend.close_all()


def _make_fake_jpg(tmp_path: Path) -> Path:
    p = tmp_path / "frame.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9")
    return p


def test_config_cli_providers_and_purpose_split(isolated_home):
    """翻译、整理、画面字复核可以分别选本机 CLI 或 OpenAI 兼容接口；本机 CLI 不需要 API Key。"""
    secrets.set("api1", "sk-api")
    cfg = config.Config.model_validate({
        "llm": {
            "translate": "agy",
            "organize": "api1",
            "vision": "codex",
            "listen": "api1",
            "providers": [
                {"name": "api1", "base_url": "https://api.example/v1", "model": "m-api", "listen_model": "gemini-audio"},
                {"name": "agy", "kind": "agy", "model": "gemini-4-neon-medium"},
                {"name": "codex", "kind": "codex", "model": "gpt-6.2-sol", "vision_model": "gpt-6.2-sol"},
            ],
        },
    })
    assert cfg.llm_mode() == "api"
    assert cfg.endpoint_ready("translate") and cfg.endpoint_ready("organize") and cfg.endpoint_ready("vision")
    assert cfg.endpoint("translate") == {"name": "agy", "kind": "agy", "command": "", "base_url": "", "model": "gemini-4-neon-medium", "key": ""}
    assert cfg.endpoint("organize") == {"name": "api1", "base_url": "https://api.example/v1", "model": "m-api", "key": "sk-api"}
    assert cfg.endpoint("vision")["kind"] == "codex" and cfg.endpoint("vision")["model"] == "gpt-6.2-sol"
    assert cfg.endpoint("listen")["model"] == "gemini-audio" and cfg.endpoint("listen").get("kind", "openai") == "openai"

    # CLI 没有填模型时不算就绪
    no_model = config.Config.model_validate({"llm": {"translate": "agy", "providers": [{"name": "agy", "kind": "agy"}]}})
    assert not no_model.endpoint_ready("translate") and no_model.llm_mode() == "export"


def test_agy_acp_worker_multimodal_effort_and_cache(tmp_path, monkeypatch):
    script = tmp_path / "mock_acp.py"
    script.write_text(MOCK_ACP_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(cli_backend, "find_executable", lambda kind, configured="": str(script))
    monkeypatch.setattr(cli_backend.AgyAcpWorker, "_spawn_cmd", lambda self: [sys.executable, "-u", str(script), "ok"])

    ms = cli_backend.list_cli_models("agy")
    assert ms["ok"] and [m["id"] for m in ms["models"]] == ["gemini-4-neon-low", "gemini-4-neon-medium", "gemini-4-neon-high"]

    cfg = config.Config.model_validate({
        "llm": {
            "translate": "agy",
            "providers": [{"name": "agy", "kind": "agy", "model": "gemini-4-neon-medium"}],
        }
    })
    img = _make_fake_jpg(tmp_path)
    llm = LLM(cfg, cache_dir=tmp_path / "cache", effort="high")
    res = llm.chat_json("你是译者", "翻译测试", images=[{"path": str(img), "label": "图 1", "detail": "low"}])
    assert res == {"ok": True, "model": "gemini-4-neon-high", "images": 1, "mime": "image/jpeg", "denied": True}
    assert llm.usage == {"prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280, "cached_tokens": 400, "reasoning_tokens": 20}

    # 第二次相同请求走缓存（usage 为 None）
    res2 = llm.chat_json("你是译者", "翻译测试", images=[{"path": str(img), "label": "图 1", "detail": "low"}])
    assert res2 == res and llm.usage is None


def test_codex_app_server_worker_multimodal_models_and_errors(tmp_path, monkeypatch):
    script = tmp_path / "mock_codex.py"
    script.write_text(MOCK_CODEX_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(cli_backend, "find_executable", lambda kind, configured="": str(script))
    monkeypatch.setattr(cli_backend.CodexAppServerWorker, "_spawn_cmd", lambda self: [sys.executable, "-u", str(script), "ok"])

    # 模型列表
    ms = cli_backend.list_cli_models("codex")
    assert ms["ok"] and ms["models"][0]["id"] == "gpt-6.2-sol" and "high" in ms["models"][0]["efforts"]

    cfg = config.Config.model_validate({
        "llm": {
            "translate": "codex",
            "providers": [{"name": "codex", "kind": "codex", "model": "gpt-6.2-sol"}],
        }
    })
    img = _make_fake_jpg(tmp_path)
    llm = LLM(cfg, effort="high")
    res = llm.chat_json("SYS_TAG 译者", "翻译测试", images=[{"path": str(img), "label": "图 1", "detail": "low"}])
    assert res == {"ok": True, "model": "gpt-6.2-sol", "effort": "high", "images": 1, "detail": "low", "has_base": True, "declined": True}
    assert llm.usage["prompt_tokens"] == 900 and llm.usage["reasoning_tokens"] == 15

    # Codex 传音频应直接报 FatalLLMError(415)
    with pytest.raises(FatalLLMError) as ei_aud:
        llm.chat_json("s", "u", audio=(str(img), "mp3"))
    assert ei_aud.value.status == 415

    # 未登录：致命错误，不重试
    cli_backend.close_all()
    monkeypatch.setattr(cli_backend.CodexAppServerWorker, "_spawn_cmd", lambda self: [sys.executable, "-u", str(script), "unauth"])
    with pytest.raises(FatalLLMError) as ei:
        llm.chat_json("s", "u")
    assert ei.value.status == 401

    # 临时限流：自动重试后成功
    cli_backend.close_all()
    monkeypatch.setattr(cli_backend.CodexAppServerWorker, "_spawn_cmd", lambda self: [sys.executable, "-u", str(script), "flaky"])
    monkeypatch.setattr("nyasub.llm.client.time.sleep", lambda _: None)
    cfg_retry = cfg.model_copy(deep=True)
    cfg_retry.llm.max_retries = 2
    res_ok = LLM(cfg_retry).chat_json("SYS_TAG", "retry")
    assert res_ok["ok"] is True


def test_cli_worker_respects_cancel_event(tmp_path, monkeypatch):
    script = tmp_path / "mock_acp.py"
    script.write_text(MOCK_ACP_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(cli_backend, "find_executable", lambda kind, configured="": str(script))
    monkeypatch.setattr(cli_backend.AgyAcpWorker, "_spawn_cmd", lambda self: [sys.executable, "-u", str(script), "slow"])

    ev = threading.Event()
    env.set_cancel(ev)
    cfg = config.Config.model_validate({
        "llm": {"translate": "agy", "providers": [{"name": "agy", "kind": "agy", "model": "gemini-4-neon-medium"}]}
    })
    threading.Timer(0.4, ev.set).start()
    t0 = time.time()
    try:
        with pytest.raises(env.Cancelled):
            LLM(cfg).chat_json("s", "u")
        assert time.time() - t0 < 3.0
    finally:
        env.set_cancel(None)


def test_audio_stages_reject_cli_provider(tmp_path):
    cfg = config.Config.model_validate({
        "llm": {"translate": "codex", "listen": "codex", "providers": [{"name": "codex", "kind": "codex", "model": "gpt-6.2-sol"}]}
    })
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    assert listen.annotate([{"id": "q1", "type": "dialogue", "start": 0.0, "end": 1.0, "orig": {"text": "あ"}}], cfg, None, wav, tmp_path) == 0
    with pytest.raises(asr_api.AsrApiError, match="不支持本机 CLI"):
        asr_api.transcribe(wav, cfg.asr.model_dump(), cfg.endpoint("asr"))


def test_agy_audio_listen_and_asr_gemini(tmp_path, monkeypatch):
    import wave

    script = tmp_path / "mock_acp.py"
    script.write_text(MOCK_ACP_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(cli_backend, "find_executable", lambda kind, configured="": str(script))

    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * 16000)

    cfg = config.Config.model_validate({
        "llm": {
            "translate": "agy",
            "listen": "agy",
            "providers": [{"name": "agy", "kind": "agy", "model": "gemini-4-hydrogen-medium", "listen_model": "gemini-4-hydrogen-medium"}],
        },
        "asr": {"backend": "api", "api_provider": "agy", "api_kind": "gemini", "api_model": "gemini-4-hydrogen-medium", "api_timing_model": "gpt-transcribe"},
    })

    # transcribe 模式仍拒绝 agy
    with pytest.raises(asr_api.AsrApiError, match="多模态大模型"):
        asr_api.transcribe(wav, {**cfg.asr.model_dump(), "api_kind": "transcribe"}, cfg.endpoint("asr"))

    # 1) audio_ok: listen.annotate + asr_api(gemini) 均成功并清理临时音频
    cli_backend.close_all()
    monkeypatch.setattr(cli_backend.AgyAcpWorker, "_spawn_cmd", lambda self: [sys.executable, "-u", str(script), "audio_ok"])
    try:
        cues = [{"id": "q1", "type": "dialogue", "start": 0.1, "end": 0.8, "orig": {"text": "何これ？"}}]
        assert listen.annotate(cues, cfg, tmp_path / "cache_listen", wav, tmp_path / "clips") == 1
        assert cues[0]["listen"] == {"kind": "问句", "tone": "轻声"}
        w = cli_backend._get_worker("agy", str(script))
        assert w._cwd and not list(Path(w._cwd).glob("audio_*"))

        segs, det_lang = asr_api.transcribe(wav, cfg.asr.model_dump(), cfg.endpoint("asr"), cache_dir=tmp_path / "cache_asr")
        assert det_lang == "ja" and len(segs) == 1 and segs[0]["text"] == "風の中に"
        assert w._cwd and not list(Path(w._cwd).glob("audio_*"))

        # 2) audio_unread: 未读取音频文件应报错
        cli_backend.close_all()
        monkeypatch.setattr(cli_backend.AgyAcpWorker, "_spawn_cmd", lambda self: [sys.executable, "-u", str(script), "audio_unread"])
        with pytest.raises(RuntimeError, match="未读取音频"):
            cli_backend.call_cli("agy", str(script), "gemini-4-hydrogen-medium", "sys", "usr", audio=(wav, "wav"))

        # 3) audio_fail: 工具读取失败应抛 FatalLLMError(415)
        cli_backend.close_all()
        monkeypatch.setattr(cli_backend.AgyAcpWorker, "_spawn_cmd", lambda self: [sys.executable, "-u", str(script), "audio_fail"])
        with pytest.raises(FatalLLMError) as ei:
            cli_backend.call_cli("agy", str(script), "gemini-4-hydrogen-medium", "sys", "usr", audio=(wav, "wav"))
        assert ei.value.status == 415
    finally:
        cli_backend.close_all()


def test_settings_api_supports_cli_and_split_purposes(isolated_home, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from nyasub.server.app import create_app

    monkeypatch.setattr(cli_backend, "detect_cli", lambda kind, configured="", check_version=True: {
        "installed": True, "path": f"/fake/{kind}", "version": "1.0", "error": "",
    })
    monkeypatch.setattr(cli_backend, "list_cli_models", lambda kind, command="": {
        "ok": True, "installed": True, "path": f"/fake/{kind}", "version": "1.0", "error": "",
        "models": [{"id": "gemini-4-neon-medium" if kind == "agy" else "gpt-6.2-sol", "name": "Mock"}],
    })
    monkeypatch.setattr(cli_backend, "test_cli_provider", lambda kind, model, command="": {
        "ok": True, "ms": 42, "reply": "好",
    })

    payload = {
        "llm": {
            "translate": "agy",
            "organize": "api1",
            "vision": "api1",
            "providers": [
                {"name": "api1", "base_url": "https://api.example/v1", "model": "gpt-6.2-sol", "vision_model": "qwen-vl-max"},
                {"name": "agy", "kind": "agy", "model": "gemini-4-neon-medium"},
                {"name": "codex", "kind": "codex", "model": "gpt-6.2-sol"},
            ],
        }
    }
    with TestClient(create_app()) as c:
        r = c.put("/api/settings", json={"user_config": payload})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["translate"]["kind"] == "agy" and body["translate"]["model"] == "gemini-4-neon-medium"
        assert body["organize"]["kind"] == "openai" and body["organize"]["model"] == "gpt-6.2-sol"
        assert body["vision"]["kind"] == "openai" and body["vision"]["model"] == "qwen-vl-max"
        assert body["cli"]["agy"]["installed"] and body["cli"]["codex"]["installed"]

        m = c.post("/api/settings/cli/models", json={"kind": "agy"}).json()
        assert m["ok"] and m["models"][0]["id"] == "gemini-4-neon-medium"

        t = c.post("/api/settings/test", json={"name": "agy"}).json()
        assert t["ok"] and t["reply"] == "好"


def test_provider_models_endpoint(isolated_home, monkeypatch):
    pytest.importorskip("fastapi")
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from fastapi.testclient import TestClient

    from nyasub.server.app import create_app

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    seen_auth: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            auth = self.headers.get("Authorization", "")
            seen_auth.append(auth)
            if self.path != "/v1/models":
                self.send_response(404)
                self.end_headers()
                return
            if auth != "Bearer sk-ok":
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": "invalid api key"}}).encode("utf-8"))
                return
            body = {
                "data": [
                    {
                        "id": "z-audio-model",
                        "name": "Z Audio Model",
                        "architecture": {"modality": "text+audio->text"},
                        "top_provider": {"context_length": 32768},
                    },
                    {
                        "id": "google/gemini-2.5-pro",
                        "name": "Google: Gemini 2.5 Pro",
                        "architecture": {"input_modalities": ["text", "image", "audio"]},
                        "context_length": 1000000,
                    },
                    {
                        "id": "gpt-4o",
                        "owned_by": "openai",
                    },
                    {
                        "id": "gpt-4o",
                        "owned_by": "duplicate",
                    },
                ]
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode("utf-8"))

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{srv.server_port}/v1"

    monkeypatch.setattr(cli_backend, "list_cli_models", lambda kind, command="": {
        "ok": True, "installed": True, "path": f"/fake/{kind}", "version": "1.2.3", "error": "",
        "models": [{"id": f"{kind}-default", "name": f"{kind} Default", "default": True}],
    })

    try:
        with TestClient(create_app()) as c:
            # 1) 直接传 base_url + key
            r1 = c.post("/api/settings/models", json={"kind": "openai", "base_url": base_url, "key": "sk-ok"}).json()
            assert r1["ok"] is True
            ids = [m["id"] for m in r1["models"]]
            assert ids == ["google/gemini-2.5-pro", "gpt-4o", "z-audio-model"]  # 已去重且按 id 排序
            by_id = {m["id"]: m for m in r1["models"]}
            assert by_id["google/gemini-2.5-pro"]["modalities"] == ["text", "image", "audio"]
            assert by_id["google/gemini-2.5-pro"]["context_length"] == 1000000
            assert by_id["z-audio-model"]["modalities"] == ["text", "audio"]
            assert by_id["z-audio-model"]["context_length"] == 32768
            assert by_id["gpt-4o"]["owned_by"] == "openai"

            # 2) 已保存接口 + 系统凭据库（实际走 isolated_home 的凭据写入）
            secrets.set("or_mock", "sk-ok")
            c.put("/api/settings", json={"user_config": {
                "llm": {
                    "translate": "or_mock",
                    "providers": [
                        {"name": "or_mock", "base_url": base_url, "model": "gpt-4o"},
                        {"name": "agy_local", "kind": "agy", "model": "agy-default"},
                    ],
                }
            }})
            r2 = c.post("/api/settings/models", json={"name": "or_mock"}).json()
            assert r2["ok"] is True and len(r2["models"]) == 3
            assert seen_auth[-1] == "Bearer sk-ok"

            # 3) 错误 Key -> HTTP 401 提示
            r3 = c.post("/api/settings/models", json={"kind": "openai", "base_url": base_url, "key": "bad"}).json()
            assert r3["ok"] is False and "HTTP 401" in r3["error"] and "invalid api key" in r3["error"]

            # 4) 空接口地址
            r4 = c.post("/api/settings/models", json={"kind": "openai", "base_url": ""}).json()
            assert r4["ok"] is False and r4["models"] == []

            # 5) 本机 CLI 分发（按名字和直接按 kind）
            r5 = c.post("/api/settings/models", json={"name": "agy_local"}).json()
            assert r5["ok"] is True and r5["models"][0]["id"] == "agy-default"
            r6 = c.post("/api/settings/models", json={"kind": "codex"}).json()
            assert r6["ok"] is True and r6["models"][0]["id"] == "codex-default"
    finally:
        srv.shutdown()


def test_step_model_and_effort_overrides(tmp_path, monkeypatch):
    import wave
    from nyasub.core import pipeline

    # 1) endpoint() 与 effort() 优先级 + 旧 legacy vision_model 不越权覆盖 providers
    cfg = config.Config.model_validate({
        "llm": {
            "translate": "api1",
            "vision_model": "legacy-vision-should-not-hijack",
            "providers": [
                {"name": "api1", "base_url": "https://api.example/v1", "model": "base-m", "vision_model": "prov-v", "listen_model": "prov-l", "api_key": "k"},
            ],
            "translate_model": "step-tr",
            "organize_model": "step-org",
            "refine_model": "",
            "listen_model": "step-li",
            "reasoning_effort": "high",
            "organize_effort": "low",
            "vision_effort": "medium",
            "listen_effort": "low",
        },
        "asr": {"api_kind": "gemini", "api_model": "step-asr", "api_effort": "low"},
    })
    assert cfg.endpoint("translate")["model"] == "step-tr"
    assert cfg.endpoint("organize")["model"] == "step-org"
    assert cfg.endpoint("vision")["model"] == "prov-v"   # 未设 refine_model 时只用 provider.vision_model，不被旧 vision_model 抢走
    assert cfg.endpoint("listen")["model"] == "step-li"
    assert cfg.endpoint("asr")["model"] == "step-asr"
    assert cfg.effort("translate") == "high"
    assert cfg.effort("organize") == "low"
    assert cfg.effort("vision") == "medium"
    assert cfg.effort("listen") == "low"
    assert cfg.effort("asr") == "low"

    cfg_over_ref = cfg.model_copy(deep=True)
    cfg_over_ref.llm.refine_model = "step-vis"
    assert cfg_over_ref.endpoint("vision")["model"] == "step-vis"

    # 2) AgyAcpWorker.list_models 推导 efforts 列表
    script = tmp_path / "mock_acp.py"
    script.write_text(MOCK_ACP_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(cli_backend, "find_executable", lambda kind, configured="": str(script))
    monkeypatch.setattr(cli_backend.AgyAcpWorker, "_spawn_cmd", lambda self: [sys.executable, "-u", str(script), "ok"])
    try:
        ms = cli_backend.list_cli_models("agy")
        assert ms["ok"] and ms["models"][0]["efforts"] == ["low", "medium", "high"]
    finally:
        cli_backend.close_all()

    # 3) listen.annotate 与 asr_api.transcribe 正确传递 step effort
    seen_calls: list[tuple[str, str]] = []

    def fake_call_cli(kind, command, model, system, user, images=None, audio=None, effort="", timeout=180.0):
        seen_calls.append((model, effort))
        if "逐字转写员" in system:
            return json.dumps({"language": "ja", "segments": [{"start": "00:00.10", "end": "00:00.80", "text": "テスト"}]}), None
        return json.dumps({"notes": [{"id": "q1", "end": "下降", "kind": "陈述", "tone": "", "heard": ""}]}), None

    monkeypatch.setattr(cli_backend, "call_cli", fake_call_cli)
    wav = tmp_path / "b.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x01\x00" * 16000)
    cfg_agy = config.Config.model_validate({
        "llm": {
            "translate": "agy",
            "listen": "agy",
            "listen_model": "gemini-4-argon-medium",
            "listen_effort": "low",
            "providers": [{"name": "agy", "kind": "agy", "model": "gemini-4-neon-medium"}],
        },
        "asr": {"backend": "api", "api_provider": "agy", "api_kind": "gemini", "api_model": "gemini-4-hydrogen-medium", "api_effort": "medium"},
    })
    cues = [{"id": "q1", "type": "dialogue", "start": 0.1, "end": 0.8, "orig": {"text": "てすと"}}]
    assert listen.annotate(cues, cfg_agy, tmp_path / "c1", wav, tmp_path / "clips") == 1
    segs, _ = asr_api.transcribe(wav, cfg_agy.asr.model_dump(), cfg_agy.endpoint("asr"), cache_dir=tmp_path / "c2")
    assert len(segs) == 1 and seen_calls == [("gemini-4-argon-medium", "low"), ("gemini-4-hydrogen-medium", "medium")]

    # 4) pipeline._extra 仅在非空时写入 effort，留空时不破坏旧指纹键
    vid = tmp_path / "ep.mp4"
    vid.write_bytes(b"mp4")
    r_plain = pipeline.Run(str(vid), config.Config.model_validate({
        "ocr": {"refine": "vlm"},
        "llm": {"listen_audio": True, "providers": [{"name": "a", "base_url": "https://x/v1", "model": "m", "vision_model": "v", "listen_model": "l", "api_key": "k"}], "translate": "a"},
    }), pipeline.Options(workdir=str(tmp_path / "w1")))
    r_plain.results["audio"] = {"wav": str(wav)}
    assert "effort" not in r_plain._extra("organize")
    assert "effort" not in r_plain._extra("ocr_refine")
    assert "listen_effort" not in r_plain._extra("translate")

    cfg_eff = cfg_agy.model_copy(deep=True)
    cfg_eff.ocr.refine = "vlm"
    cfg_eff.llm.listen_audio = True
    cfg_eff.llm.organize_effort = "low"
    cfg_eff.llm.vision_effort = "medium"
    cfg_eff.llm.reasoning_effort = "high"
    r_eff = pipeline.Run(str(vid), cfg_eff, pipeline.Options(workdir=str(tmp_path / "w2")))
    r_eff.results["audio"] = {"wav": str(wav)}
    assert r_eff._extra("asr")["api"]["effort"] == "medium"
    assert r_eff._extra("ocr_refine")["effort"] == "medium"
    assert r_eff._extra("organize")["effort"] == "low"
    assert r_eff._extra("translate")["listen_effort"] == "low"
    assert r_eff._extra("translate")["effort"] == "high"
