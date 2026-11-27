"""双层术语表管理（作品共享 + 单集独有）与旧版膨胀术语表自动迁移。

设计原则：
- 作品级（<home>/series/<series>/glossary.json）：存放跨集复用术语（manual / import / edited / 连续剧常驻词）。
- 单集级（<proj>.nyasub/glossary.json）：存放本集自动提取的全部术语，不反向污染作品级。
- 回写策略由 ProjectCfg.glossary_sync 与 ProjectCfg.content_kind 共同决定：
  - documentary / variety（纪录片、单元剧）在 auto 模式下默认仅存单集，避免一次性受访者人名膨胀作品表；
  - anime / drama（动画、连续剧）以及未指定（auto）默认沉淀常驻条目到作品级；
  - 始终将本集完整术语存一份在单集级，方便单集查阅与独立复现。
- 旧版自动迁移：首次读取 version < 2 且包含多集膨胀自动条目的作品术语表时，先备份到
  <series>/backup/glossary.v1.json，仅保留 manual / import / edited / scope=series 及跨 >=2 集共现的条目，
  其余一次性单集自动条目归档至 backup/glossary.v1.archived_episode_terms.json。
"""
from collections import Counter
from pathlib import Path

from ..env import log
from .store import load_json, save_json

SCHEMA_VERSION = 2
USER_SOURCES = ("manual", "import")
ALLOWED_FIELDS = ("src", "reading", "tr", "type", "note", "from", "edited", "aliases", "relations", "scope")


def clean_term(t: dict, default_from: str | None = None) -> dict | None:
    if not isinstance(t, dict) or not (t.get("src") or "").strip():
        return None
    out = {}
    for k in ALLOWED_FIELDS:
        v = t.get(k)
        if k == "aliases":
            if isinstance(v, list):
                cleaned = [str(x).strip() for x in v if str(x).strip()]
                if cleaned:
                    out[k] = cleaned
            continue
        if v not in (None, "", False):
            out[k] = v
    if default_from and not out.get("from"):
        out["from"] = default_from
    return out


def should_sync_to_series(cfg) -> bool:
    """本集自动提取的术语是否回写到作品级术语表。"""
    if not (getattr(cfg.project, "series", "") or "").strip():
        return False
    sync = getattr(cfg.project, "glossary_sync", "auto") or "auto"
    if sync == "series":
        return True
    if sync == "episode":
        return False
    kind = getattr(cfg.project, "content_kind", "auto") or "auto"
    return kind not in ("documentary", "variety")


def is_user_term(t: dict) -> bool:
    return t.get("from") in USER_SOURCES or bool(t.get("edited")) or t.get("scope") == "series"


def migrate_series_glossary(path: Path) -> dict:
    """读取作品级术语表；若是旧版多集膨胀表（version < 2），自动备份并精简迁移。"""
    if not path.exists():
        return {"version": SCHEMA_VERSION, "terms": []}
    data = load_json(path, {"terms": []})
    if not isinstance(data, dict):
        return {"version": SCHEMA_VERSION, "terms": []}
    terms = data.get("terms") or []
    if data.get("version", 1) >= SCHEMA_VERSION:
        return {"version": data.get("version", SCHEMA_VERSION), "terms": terms}

    # 统计非人工来源的不同集数
    auto_sources = {t.get("from") for t in terms if t.get("from") and not is_user_term(t)}
    # 仅在多集混杂且明显膨胀（>=2 集自动术语且总条数 > 80）时做拆分归档；
    # 单集或小规模作品直接升级版本号，不丢失已有条目。
    if len(auto_sources) < 2 or len(terms) <= 80:
        upgraded = {"version": SCHEMA_VERSION, "terms": terms}
        save_json(path, upgraded)
        return upgraded

    backup_dir = path.parent / "backup"
    backup_v1 = backup_dir / "glossary.v1.json"
    if not backup_v1.exists():
        save_json(backup_v1, data)

    # 统计同一原文在多少不同集里出现（跨集共现视为真正复用的作品术语）
    src_episodes: dict[str, set[str]] = {}
    for t in terms:
        src = (t.get("src") or "").strip()
        frm = t.get("from") or ""
        if src and frm and frm not in USER_SOURCES:
            src_episodes.setdefault(src, set()).add(frm)
    # 有些旧数据里同名术语只留了一条，也检查同 tr 跨集出现次数较少的普通情况；这里以 src 跨集共现为准
    multi_ep = {src for src, eps in src_episodes.items() if len(eps) >= 2}

    kept, archived = [], []
    seen_kept = set()
    for t in terms:
        src = (t.get("src") or "").strip()
        if not src:
            continue
        if is_user_term(t) or src in multi_ep:
            if src not in seen_kept:
                seen_kept.add(src)
                kept.append(t)
        else:
            archived.append(t)

    if archived:
        save_json(backup_dir / "glossary.v1.archived_episode_terms.json", {
            "version": 1,
            "archived_from": str(path),
            "episodes": Counter(t.get("from") or "" for t in archived),
            "terms": archived,
        })
    upgraded = {"version": SCHEMA_VERSION, "terms": kept}
    save_json(path, upgraded)
    log(f"作品术语表自动升级：保留 {len(kept)} 条共享术语，归档 {len(archived)} 条单集一次性术语（已备份至 backup/）")
    return upgraded


def load_series_terms(series_path: Path | None) -> list[dict]:
    if not series_path:
        return []
    return list(migrate_series_glossary(series_path).get("terms") or [])


def load_episode_terms(episode_path: Path) -> list[dict]:
    if not episode_path.exists():
        return []
    return list(load_json(episode_path, {"terms": []}).get("terms") or [])


def merge_tiered_terms(series_terms: list[dict], episode_terms: list[dict], episode_stem: str) -> list[dict]:
    """合并「作品共享术语」与「本集确认/人工术语」（排除本集未编辑的旧自动条目，避免指纹循环）。"""
    out, seen = [], set()
    for t in series_terms:
        src = (t.get("src") or "").strip()
        if not src or src in seen:
            continue
        if t.get("from") == episode_stem and not t.get("edited"):
            continue
        seen.add(src)
        out.append(t)
    for t in episode_terms:
        src = (t.get("src") or "").strip()
        if not src or src in seen:
            continue
        if t.get("from") == episode_stem and not t.get("edited"):
            continue
        seen.add(src)
        out.append(t)
    return out


def save_episode_glossary(episode_path: Path, terms: list[dict], episode_stem: str) -> int:
    """保存本集完整术语表（保留本集内手工/编辑过的条目，其余替换为本次结果）。返回新增条数。"""
    saved = load_episode_terms(episode_path)
    keep = [t for t in saved if not (t.get("from") == episode_stem and not t.get("edited"))]
    known = {t.get("src") for t in keep}
    new = [dict(t, **{"from": episode_stem}) for t in terms if t.get("src") and t.get("src") not in known]
    if not new and len(keep) == len(saved) and episode_path.exists():
        return 0
    save_json(episode_path, {"version": SCHEMA_VERSION, "terms": keep + new})
    return len(new)


def save_series_glossary(series_path: Path, terms: list[dict], episode_stem: str) -> tuple[int, int]:
    """将本集术语回写到作品级术语表（替换本集旧自动条目，保留其他集和手工条目）。返回 (新增条数, 总条数)。"""
    saved = load_series_terms(series_path)
    keep = [t for t in saved if not (t.get("from") == episode_stem and not t.get("edited"))]
    known = {t.get("src") for t in keep}
    new = [dict(t, **{"from": episode_stem}) for t in terms if t.get("src") and t.get("src") not in known]
    if not new and len(keep) == len(saved):
        return 0, len(saved)
    save_json(series_path, {"version": SCHEMA_VERSION, "terms": keep + new})
    return len(new), len(keep) + len(new)
