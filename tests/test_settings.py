"""全局设置：多个大模型接口、API Key 进凭据库、测试连接。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from nyasub import secrets  # noqa: E402
from nyasub.core import config  # noqa: E402
from nyasub.server.app import create_app  # noqa: E402

SETTINGS = {"llm": {"translate": "deepseek", "vision": "bailian", "providers": [
    {"name": "deepseek", "base_url": "http://127.0.0.1:1/v1", "model": "deepseek-chat", "api_key": "不该被保存"},
    {"name": "bailian", "base_url": "https://dashscope.example/v1", "model": "qwen-max", "vision_model": "qwen-vl-max"},
]}}


def test_endpoint_resolution_and_keys():
    llm = json.loads(json.dumps(SETTINGS["llm"]))
    llm["providers"][0].pop("api_key")  # 这里只测凭据库
    cfg = config.Config.model_validate({"llm": llm})
    assert cfg.llm_mode() == "export"  # 还没有 Key
    secrets.set("deepseek", "sk-1")
    assert cfg.llm_mode() == "api"
    assert cfg.endpoint() == {"name": "deepseek", "base_url": "http://127.0.0.1:1/v1", "model": "deepseek-chat", "key": "sk-1"}
    v = cfg.endpoint("vision")
    assert (v["name"], v["model"], v["key"]) == ("bailian", "qwen-vl-max", "")
    # 旧写法照常可用
    old = config.Config.model_validate({"llm": {"model": "m", "api_key": "k"}})
    assert old.endpoint()["model"] == "m" and old.llm_mode() == "api"


def test_settings_api_roundtrip(isolated_home):
    with TestClient(create_app()) as c:
        r = c.put("/api/settings", json={"user_config": SETTINGS})
        assert r.status_code == 200, r.text
        saved = (isolated_home / "config.toml").read_text(encoding="utf-8")
        assert "deepseek" in saved and "不该被保存" not in saved  # Key 不写进配置文件
        assert r.json()["keys"] == {"deepseek": False, "bailian": False}
        assert c.put("/api/settings/keys/deepseek", json={"key": "sk-x"}).json()["ok"]
        s = c.get("/api/settings").json()
        assert s["keys"]["deepseek"] is True and "sk-x" not in json.dumps(s)  # 只返回有没有，不回显
        assert s["translate"]["model"] == "deepseek-chat"
        c.delete("/api/settings/keys/deepseek")
        assert c.get("/api/settings").json()["keys"]["deepseek"] is False
        assert c.put("/api/settings", json={"user_config": {"llm": {"mode": "maybe"}}}).status_code == 400


class _Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ok = self.headers.get("Authorization") == "Bearer sk-good"
        out = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                          "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "好"}}]}
                         if ok else {"error": {"message": "invalid key"}}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_connection_test_against_mock():
    srv = HTTPServer(("127.0.0.1", 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    try:
        with TestClient(create_app()) as c:
            good = c.post("/api/settings/test", json={"base_url": url, "model": "m", "key": "sk-good"}).json()
            assert good["ok"] and good["reply"] == "好"
            bad = c.post("/api/settings/test", json={"base_url": url, "model": "m", "key": "sk-bad"}).json()
            assert not bad["ok"] and "401" in bad["error"]
            assert c.post("/api/settings/test", json={"name": "nope"}).json()["ok"] is False
    finally:
        srv.shutdown()
