"""OpenAI 兼容接口（OpenAI / DeepSeek / 阿里百炼 / OpenRouter / 各类中转 / 本地 Ollama）。

每次请求按内容哈希缓存在项目的 llm_cache/ 里：中断重跑、或上游没变时不会重复花钱。
"""
import base64
import hashlib
import json
import re
import time
from pathlib import Path

from ..core.store import digest, load_json, save_json
from ..env import check_cancel, fresh, log


class FatalLLMError(RuntimeError):
    """配置类错误（模型不存在、key 无效等），不重试。status：接口返回的状态码。"""
    status = None


class LLM:
    def __init__(self, cfg, vision=False, cache_dir=None, effort="", purpose=""):
        """effort：思考程度（请求里的 reasoning_effort，比如 low / medium / high），空 = 不设、用接口默认。
        purpose：用哪个接口（translate / vision / listen），空 = 按 vision 选翻译或视觉复核的接口。"""
        from openai import OpenAI

        c = cfg.llm
        self.c = c
        self.effort = (effort or "").strip()
        ep = cfg.endpoint(purpose or ("vision" if vision else "translate"))
        self.model = ep["model"]
        if not ep["key"] or not self.model:
            raise RuntimeError("没有配置大模型接口：请在「设置」里添加接口并设置 API Key（或在配置的 [llm] 里填 base_url / model）")
        self.client = OpenAI(base_url=ep["base_url"], api_key=ep["key"], timeout=c.timeout)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.usage = None  # 最近一次真正发出去的请求的用量（读缓存时是 None）

    def chat_json(self, system, user, images=None, audio=None):
        """images：图片路径，或 {"path", "label"（图前面的说明文字）, "detail"（low / high）}。
        audio：(音频文件, 格式 mp3 / wav)，附在提示词后面（chat/completions 的 input_audio）。"""
        img_hashes = [hashlib.sha1(Path(p).read_bytes()).hexdigest() if not isinstance(p, dict)
                      else [p.get("label"), hashlib.sha1(Path(p["path"]).read_bytes()).hexdigest(), p.get("detail")]
                      for p in images or []]
        key = digest({"model": self.model, "t": self.c.temperature, "system": system, "user": user, "img": img_hashes,
                      **({"effort": self.effort} if self.effort else {}),  # 不设思考程度时和以前的缓存一样
                      **({"audio": hashlib.sha1(Path(audio[0]).read_bytes()).hexdigest()} if audio else {})})
        cache = self.cache_dir / f"{key}.json" if self.cache_dir else None
        self.usage = None
        if cache and cache.exists() and not fresh():
            return load_json(cache)
        result = self._call(system, user, images, **({"audio": audio} if audio else {}))
        if cache:
            save_json(cache, result)
        return result

    def _call(self, system, user, images, audio=None):
        content = user
        if images or audio:
            content = [{"type": "text", "text": user}]
            for img in images or []:
                img = img if isinstance(img, dict) else {"path": img}
                if img.get("label"):
                    content.append({"type": "text", "text": img["label"]})
                b64 = base64.b64encode(Path(img["path"]).read_bytes()).decode()
                url = {"url": f"data:image/jpeg;base64,{b64}", **({"detail": img["detail"]} if img.get("detail") else {})}
                content.append({"type": "image_url", "image_url": url})
            if audio:
                b64 = base64.b64encode(Path(audio[0]).read_bytes()).decode()
                content.append({"type": "input_audio", "input_audio": {"data": b64, "format": audio[1]}})
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        last_err = None
        json_mode = self.c.json_mode
        for attempt in range(int(self.c.max_retries) + 1):
            check_cancel()  # 翻译 / 复核分很多次请求：每次请求前检查停止按钮
            try:
                kw = {"model": self.model, "messages": messages, "temperature": self.c.temperature}
                if json_mode:
                    kw["response_format"] = {"type": "json_object"}
                if self.effort:
                    kw["reasoning_effort"] = self.effort
                resp = self.client.chat.completions.create(**kw)
                self.usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
                return parse_json(resp.choices[0].message.content or "")
            except Exception as e:
                last_err = e
                msg = str(e)
                if json_mode and ("response_format" in msg or "json_object" in msg):
                    json_mode = False  # 有些中转不支持 JSON 模式
                    continue
                if self.effort and "reasoning" in msg.lower() and getattr(e, "status_code", None) in (400, 422):
                    log(f"  接口不接受思考程度「{self.effort}」（{msg[:150]}），改为不设")
                    self.effort = ""  # 这个模型不支持，或者没有这一档：后面的请求也不设
                    continue
                status = getattr(e, "status_code", None)
                if status in (400, 401, 403, 404, 415, 422):
                    err = FatalLLMError(f"大模型接口报错（{status}）：{msg[:300]}")
                    err.status = status
                    raise err from e
                if attempt == int(self.c.max_retries):
                    break
                wait = min(60, 3 * 2 ** attempt)
                log(f"  接口调用失败（{msg[:200]}），{wait}s 后重试")
                time.sleep(wait)
        raise RuntimeError(f"大模型接口多次失败：{last_err}")


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    # 字幕习惯写的 \N 换行不是合法 JSON 转义，换成 \n
    text = re.sub(r"(?<!\\)((?:\\\\)*)\\N", r"\1\\n", text)
    try:
        return _unescape(json.loads(text))
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        return _unescape(json.loads(m.group(0)))
    raise ValueError(f"返回内容不是 JSON：{text[:300]}")


def _unescape(v):
    """大模型有时把换行符多转义一层（JSON 里写成 \\\\n），解析出来是字面的“\\n”：换回换行。"""
    if isinstance(v, str):
        return re.sub(r"\\[nN]", "\n", v)
    if isinstance(v, list):
        return [_unescape(x) for x in v]
    if isinstance(v, dict):
        return {k: _unescape(x) for k, x in v.items()}
    return v
