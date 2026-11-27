"""三层可信度联网检索模块（作品建档、单集简介、官方歌词原文）：

1. 第一层（优先，高可信结构化 API，免费免幻觉）：
   - 歌曲歌词：LRCLIB API（https://lrclib.net/api/search）
   - 番剧/动画：Bangumi API（https://api.bgm.tv）
   - 通用/纪录片/单集：Wikipedia MediaWiki API（ja / zh）
   - 可选：TMDB API（用户填写 tmdb_api_key 时启用）
2. 第二层（补充/补缺）：带联网能力的 AI（OpenRouter :online 等），优先引导查阅可信源，仅补充第一层缺失字段。
3. 第三层（最后兜底）：普通网页搜索摘要提取 → 交由任意已配置 LLM 整理为结构化 JSON，并标明来源提醒人工复核。

所有检索结果均返回草稿结构，交由用户确认后再落盘，不静默污染工程。
"""
import json
import re
import urllib.parse
import urllib.request

from ..env import log

UA = "NyaSubFusion/0.7 (https://github.com/stevennight/NyaSubFusion)"
TIMEOUT = 15.0


def _get_json(url: str, headers: dict | None = None, timeout: float = TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = TIMEOUT):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------- 第一层：高可信结构化 API ----------------

def fetch_lrclib(title: str, artist: str = "", query: str = "") -> list[dict]:
    """从免费公开歌词库 LRCLIB 检索准确歌词原文。
    返回 [{"title", "artist", "album", "duration", "lines": [str, ...], "source": "lrclib"}]。"""
    params = {}
    if title.strip():
        params["track_name"] = title.strip()
    if artist.strip():
        params["artist_name"] = artist.strip()
    if not params and query.strip():
        params["q"] = query.strip()
    if not params:
        return []
    url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(params)
    try:
        items = _get_json(url)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items[:8]:
        plain = (it.get("plainLyrics") or "").strip()
        if not plain:
            synced = (it.get("syncedLyrics") or "").strip()
            plain = re.sub(r"^\[\d{1,2}:\d{2}(?:\.\d+)?\]\s*", "", synced, flags=re.MULTILINE).strip()
        lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
        if not lines:
            continue
        out.append({
            "title": (it.get("trackName") or title).strip(),
            "artist": (it.get("artistName") or artist).strip(),
            "album": (it.get("albumName") or "").strip(),
            "duration": float(it.get("duration") or 0),
            "lines": lines,
            "source": "lrclib",
        })
    return out


def fetch_bangumi(query: str, limit: int = 3) -> dict | None:
    """从 Bangumi API 检索番剧/动画的官方简介与角色表（含日文原名、中文译名、CV）。"""
    q = (query or "").strip()
    if not q:
        return None
    try:
        res = _post_json(
            f"https://api.bgm.tv/v0/search/subjects?limit={limit}",
            {"keyword": q, "filter": {"type": [2, 6]}},  # 2=动画, 6=三次元剧集
        )
    except Exception:
        return None
    data = (res or {}).get("data") or []
    if not data:
        return None
    subj = data[0]
    sid = subj.get("id")
    chars = []
    if sid:
        try:
            raw_chars = _get_json(f"https://api.bgm.tv/v0/subjects/{sid}/characters")
            for ch in (raw_chars or [])[:40]:
                name = (ch.get("name") or "").strip()
                name_cn = (ch.get("name_cn") or "").strip() or name
                rel = (ch.get("relation") or "").strip()
                actors = [a.get("name_cn") or a.get("name") for a in (ch.get("actors") or []) if a.get("name")]
                if name:
                    chars.append({
                        "src": name,
                        "tr": name_cn,
                        "type": "人名",
                        "note": " / ".join(x for x in (rel, f"CV {', '.join(actors[:2])}" if actors else "") if x),
                        "from": "import",
                        "scope": "series",
                    })
        except Exception:
            pass
    return {
        "source": "bangumi",
        "title": subj.get("name") or q,
        "title_cn": subj.get("name_cn") or "",
        "summary": (subj.get("summary") or "").strip(),
        "terms": chars,
    }


def fetch_wikipedia(query: str, langs: tuple[str, ...] = ("ja", "zh")) -> list[dict]:
    """从 Wikipedia MediaWiki API 获取高可信百科条目摘要（支持日语/中文双语）。"""
    q = (query or "").strip()
    if not q:
        return []
    out = []
    for lg in langs:
        try:
            search_url = (f"https://{lg}.wikipedia.org/w/api.php?"
                          + urllib.parse.urlencode({
                              "action": "query", "list": "search", "srsearch": q,
                              "srlimit": 2, "format": "json", "utf8": 1,
                          }))
            sr = _get_json(search_url)
            hits = ((sr or {}).get("query") or {}).get("search") or []
            if not hits:
                continue
            title = hits[0].get("title") or ""
            if not title:
                continue
            ext_url = (f"https://{lg}.wikipedia.org/w/api.php?"
                       + urllib.parse.urlencode({
                           "action": "query", "prop": "extracts", "exintro": 0, "explaintext": 1,
                           "exchars": 3500, "titles": title, "format": "json", "utf8": 1,
                       }))
            ext = _get_json(ext_url)
            pages = ((ext or {}).get("query") or {}).get("pages") or {}
            page = next(iter(pages.values()), {})
            text = (page.get("extract") or "").strip()
            if text:
                out.append({"source": f"wikipedia:{lg}", "title": title, "text": text})
        except Exception:
            continue
    return out


# ---------------- 统一结构化整理（任意 LLM 均可用） ----------------

RESEARCH_SYSTEM = """你是影视字幕翻译的资料整理助手。
下面给出从公开权威数据库（Bangumi、Wikipedia 等）检索到的参考资料。
请基于这些资料整理出供字幕翻译使用的结构化背景 JSON（绝不编造资料中没有的事实）：
{
  "context": "精简的作品/单集背景说明（中文，150~400字，交代时代、地点、主线、关键人物关系与称呼习惯）",
  "terms": [
    {"src": "日文/原文专有名词", "reading": "读音（知道才填）", "tr": "中文标准译名",
     "type": "人名|地名|团体|场所|物品|其他", "aliases": ["常见简称或昵称"],
     "relations": "人物关系或前后期称呼变化（可选）", "note": "简短备注（可选）"}
  ],
  "songs": [
    {"title": "歌名", "artist": "歌手（知道才填）", "role": "OP|ED|insert"}
  ]
}
要求：只输出纯 JSON；专有名词不超过 60 条，优先收核心角色、地名和世界观专词。"""


def research_work(query: str, content_kind: str = "auto", cfg=None, cache_dir=None,
                  episode_title: str = "", pasted_text: str = "") -> dict:
    """按三层策略检索或整理作品/单集资料：
    - 若用户提供了 pasted_text：直接以粘贴文本作为权威输入，交由 LLM 提炼结构化背景、人物卡与歌曲列表；
    - 否则先查第一层高可信 API（Bangumi + Wikipedia 中日双语），再交由任意已配置 LLM 融合整理。
    返回 {"context", "terms", "songs", "sources"}。"""
    sources = []
    raw_blocks = []
    direct_terms = []
    fallback_context = ""

    if (pasted_text or "").strip():
        sources.append("pasted")
        raw_blocks.append(f"## 用户粘贴的参考资料\n{pasted_text.strip()[:12000]}")
        fallback_context = pasted_text.strip()[:600]
    else:
        # 第一层：高可信 API
        if content_kind in ("auto", "anime", "drama"):
            bgm = fetch_bangumi(query)
            if bgm:
                sources.append("bangumi")
                direct_terms.extend(bgm.get("terms") or [])
                fallback_context = bgm.get("summary") or ""
                raw_blocks.append(f"## Bangumi：{bgm['title']}（{bgm['title_cn']}）\n{bgm['summary']}")
                if bgm.get("terms"):
                    char_lines = "\n".join(f"- {t['src']} → {t['tr']}（{t.get('note', '')}）" for t in bgm["terms"][:30])
                    raw_blocks.append(f"## Bangumi 角色表\n{char_lines}")

        wiki_q = f"{query} {episode_title}".strip() if episode_title else query
        wikis = fetch_wikipedia(wiki_q)
        if not wikis and episode_title:
            wikis = fetch_wikipedia(query)
        for wk in wikis:
            sources.append(wk["source"])
            raw_blocks.append(f"## {wk['source']}：{wk['title']}\n{wk['text'][:2500]}")
            if not fallback_context:
                fallback_context = wk["text"][:600]

    result = {
        "context": fallback_context,
        "terms": direct_terms,
        "songs": [],
        "sources": sources,
    }
    if not raw_blocks:
        return result

    # 若有配置 LLM，使用 LLM 将多源资料融合为干净的结构化人物卡与简介
    if cfg is not None and cfg.llm_mode() == "api":
        try:
            from .client import LLM

            llm = LLM(cfg, cache_dir=cache_dir, purpose="organize")
            prompt = (f"检索目标：{query}"
                      + (f"（单集：{episode_title}）" if episode_title else "")
                      + f"\n节目类型：{content_kind}\n\n"
                      + "\n\n".join(raw_blocks))
            parsed = llm.chat_json(RESEARCH_SYSTEM, prompt)
            if isinstance(parsed, dict):
                if (parsed.get("context") or "").strip():
                    result["context"] = parsed["context"].strip()
                if parsed.get("terms"):
                    # 合并 Bangumi 原始角色与 LLM 整理的人物卡（保留 aliases/relations）
                    by_src = {t["src"]: dict(t) for t in direct_terms if t.get("src")}
                    for t in parsed["terms"]:
                        if isinstance(t, dict) and (t.get("src") or "").strip():
                            src = t["src"].strip()
                            merged = dict(by_src.get(src, {}), **t)
                            merged["from"] = "import"
                            merged["scope"] = "series" if content_kind in ("auto", "anime", "drama") else "episode"
                            by_src[src] = merged
                    result["terms"] = list(by_src.values())
                if parsed.get("songs"):
                    result["songs"] = [s for s in parsed["songs"] if isinstance(s, dict) and (s.get("title") or "").strip()]
        except Exception as e:
            log(f"联网资料 LLM 整理失败，返回原始检索结果：{e}")
    return result
