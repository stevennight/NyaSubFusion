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

_LRC_LINE = re.compile(r"^((?:\[\d{1,2}:\d{2}(?:\.\d+)?\])+)\s*(.*)$")
_LRC_TS = re.compile(r"\[(\d{1,2}):(\d{2}(?:\.\d+)?)\]")


def parse_lrc(synced: str) -> list[dict]:
    """解析带时间戳的 LRC 歌词：返回 [{"orig": str, "t": float}, ...]（按时间排序）。"""
    rows = []
    for raw in (synced or "").splitlines():
        m = _LRC_LINE.match(raw.strip())
        if not m:
            continue
        text = m.group(2).strip()
        if not text:
            continue
        for tm in _LRC_TS.finditer(m.group(1)):
            sec = int(tm.group(1)) * 60 + float(tm.group(2))
            rows.append({"orig": text, "t": round(sec, 3)})
    rows.sort(key=lambda r: r["t"])
    return rows


def fetch_lrclib(title: str, artist: str = "", query: str = "") -> list[dict]:
    """从免费公开歌词库 LRCLIB 检索准确歌词原文（优先保留 syncedLyrics 的精确逐句时间戳）。
    返回 [{"title", "artist", "album", "duration", "lines": [str, ...], "timed_lines": [{"orig", "t"}], "source": "lrclib"}]。"""
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
        synced = (it.get("syncedLyrics") or "").strip()
        timed = parse_lrc(synced) if synced else []
        plain = (it.get("plainLyrics") or "").strip()
        if timed:
            lines = [r["orig"] for r in timed]
        elif plain:
            lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
        else:
            continue
        if not lines:
            continue
        out.append({
            "title": (it.get("trackName") or title).strip(),
            "artist": (it.get("artistName") or artist).strip(),
            "album": (it.get("albumName") or "").strip(),
            "duration": float(it.get("duration") or 0),
            "lines": lines,
            "timed_lines": timed,
            "source": "lrclib",
        })
    # 优先排带精确 LRC 时间戳的候选
    out.sort(key=lambda x: (not bool(x["timed_lines"]), -len(x["lines"])))
    return out


def _clean_query(q: str) -> str:
    return re.sub(r"[\-－―—–·•:：/\\]+", " ", (q or "")).strip()


def _best_title_hit(query: str, items: list[dict], title_fn):
    from rapidfuzz import fuzz

    from ..textutil import norm

    nq = norm(query)
    if not items or not nq:
        return items[0] if items else None

    def score(it):
        cands = [norm(t) for t in title_fn(it) if t]
        return max((fuzz.ratio(nq, c) for c in cands), default=0.0)

    return max(items, key=score)


def fetch_bangumi(query: str, limit: int = 10) -> dict | None:
    """从 Bangumi API 检索番剧/动画的官方简介与角色表（含日文原名、中文译名、CV）。"""
    q = _clean_query(query)
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
    subj = _best_title_hit(query, data, lambda x: (x.get("name") or "", x.get("name_cn") or ""))
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
                if name and name not in ("モブキャラクター", "モブ", "路人"):
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
    q = _clean_query(query)
    if not q:
        return []
    out = []
    for lg in langs:
        try:
            search_url = (f"https://{lg}.wikipedia.org/w/api.php?"
                          + urllib.parse.urlencode({
                              "action": "query", "list": "search", "srsearch": q,
                              "srlimit": 5, "format": "json", "utf8": 1,
                          }))
            sr = _get_json(search_url)
            hits = ((sr or {}).get("query") or {}).get("search") or []
            if not hits:
                continue
            best = _best_title_hit(query, hits, lambda x: (x.get("title") or "",))
            title = (best or {}).get("title") or ""
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


ONLINE_SUPPLEMENT_HINT = (
    "\n\n如果上面提供的结构化资料缺失、或缺少核心角色称呼关系/OP和ED歌名，"
    "请优先查阅高可信源（Bangumi、日文/中文 Wikipedia、NHK 官网、萌娘百科、Uta-Net 等）补全缺失信息，绝不编造。"
)

LYRICS_ONLINE_SYSTEM = """你是歌词核对助手。请联网查询指定歌曲的官方标准歌词原文（不要翻译，不要加时间轴，不要编造未查到的词）。
如果查不到确切官方歌词，lines 返回空数组 []。
只输出 JSON：{"title": "歌名", "artist": "歌手", "lines": ["第一句", "第二句"]}"""


def _resolve_research_cfg(cfg, provider: str = ""):
    if cfg is None or not (provider or "").strip():
        return cfg
    c2 = cfg.model_copy(deep=True)
    c2.llm.organize = provider.strip()
    c2.llm.organize_model = ""
    return c2


def _call_online_llm(cfg, system: str, prompt: str, cache_dir=None) -> tuple[dict | None, str]:
    """使用带联网能力的接口（agy/codex 只读搜索，或 OpenRouter :online）请求 JSON。返回 (parsed_dict, source_tag)。"""
    from .client import LLM, parse_json

    ep = cfg.endpoint("organize")
    ep_kind = ep.get("kind", "openai")
    if ep_kind in ("agy", "codex") and ep.get("model"):
        from . import cli_backend

        text, _ = cli_backend.call_cli(
            ep_kind, ep.get("command") or "", ep["model"], system, prompt, timeout=180.0, allow_web=True,
        )
        return parse_json(text or ""), f"agent:{ep_kind}"
    if ep_kind == "openai" and "openrouter.ai" in (ep.get("base_url") or "") and ep.get("model"):
        c2 = cfg.model_copy(deep=True)
        model = ep["model"]
        if not model.endswith(":online"):
            c2.llm.organize_model = model + ":online"
        llm = LLM(c2, cache_dir=cache_dir, purpose="organize")
        return llm.chat_json(system, prompt), "openrouter:online"
    return None, ""


def fetch_lyrics_online(title: str, artist: str = "", cfg=None, cache_dir=None, provider: str = "") -> list[str]:
    """第二层歌词兜底：当 LRCLIB 查不到冷门歌时，若接口支持联网（agy / OpenRouter :online），联网查官方歌词原文。"""
    if cfg is None or cfg.llm_mode() != "api" or not (title or "").strip():
        return []
    rcfg = _resolve_research_cfg(cfg, provider)
    try:
        prompt = f"请查询歌曲官方歌词原文：\n歌名：{title.strip()}\n歌手：{(artist or '未知').strip()}"
        parsed, src_tag = _call_online_llm(rcfg, LYRICS_ONLINE_SYSTEM, prompt, cache_dir=cache_dir)
        if isinstance(parsed, dict) and parsed.get("lines"):
            lines = [str(x).strip() for x in parsed["lines"] if str(x).strip()]
            if lines:
                log(f"歌词联网兜底（{src_tag}）：获取到《{title}》{len(lines)} 句歌词")
                return lines
    except Exception as e:
        log(f"歌词联网兜底跳过：{e}")
    return []


def research_work(query: str, content_kind: str = "auto", cfg=None, cache_dir=None,
                  episode_title: str = "", pasted_text: str = "",
                  provider: str = "", online_fallback: bool = True) -> dict:
    """按三层策略检索或整理作品/单集资料：
    - 若用户提供了 pasted_text：直接以粘贴文本作为权威输入，交由所选 LLM 提炼结构化背景、人物卡与歌曲列表；
    - 否则先查第一层高可信 API（Bangumi + Wikipedia 中日双语）；
    - 若第一层未命中或开启 online_fallback 且所选接口支持联网（agy / codex / OpenRouter :online），
      走第二层联网补缺；否则由普通 LLM 整理第一层资料。
    返回 {"context", "terms", "songs", "sources"}。"""
    rcfg = _resolve_research_cfg(cfg, provider)
    sources = []
    raw_blocks = []
    direct_terms = []
    fallback_context = ""
    is_pasted = bool((pasted_text or "").strip())

    if is_pasted:
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

    # 若有配置 LLM：无第一层结果且允许联网补缺 → 第二层主动联网；有第一层结果 → 整理（若开启联网补缺且支持联网，可顺带补缺）
    if rcfg is not None and rcfg.llm_mode() == "api":
        base_prompt = (f"检索目标：{query}"
                       + (f"（单集：{episode_title}）" if episode_title else "")
                       + f"\n节目类型：{content_kind}\n\n"
                       + ("\n\n".join(raw_blocks) if raw_blocks else "（第一层结构化数据库未检索到条目）"))
        parsed = None
        if not is_pasted and online_fallback and (not raw_blocks or not direct_terms):
            try:
                parsed, online_tag = _call_online_llm(rcfg, RESEARCH_SYSTEM, base_prompt + ONLINE_SUPPLEMENT_HINT, cache_dir=cache_dir)
                if parsed and online_tag:
                    sources.append(online_tag)
            except Exception as e:
                log(f"联网补缺跳过（回退普通整理）：{e}")
        if parsed is None and raw_blocks:
            try:
                from .client import LLM

                llm = LLM(rcfg, cache_dir=cache_dir, purpose="organize")
                parsed = llm.chat_json(RESEARCH_SYSTEM, base_prompt)
            except Exception as e:
                log(f"联网资料 LLM 整理失败，返回原始检索结果：{e}")
        if isinstance(parsed, dict):
            if (parsed.get("context") or "").strip():
                result["context"] = parsed["context"].strip()
            if parsed.get("terms"):
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
    return result
