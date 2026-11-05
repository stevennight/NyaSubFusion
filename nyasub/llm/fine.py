"""分组按顺序翻（界面上的“精翻”= 分组翻 + 思考程度 high）：代替普通翻译，把整理好的字幕（只有原文）翻一遍。
1. 人物表：读一遍整集原文，把同一个人 / 动物的各种叫法（全名、名字、昵称、呼唤时的音变）连成一条，各给译法；
2. 分组：按“同一段对话 / 同一个话题”把连续的字幕分成几句一组；
3. 从头到尾一组一组翻：带前文（前面已经翻好的译文）、后文、前面相同 / 相近原文的译法、这几句的画面，模型每次只管这几句。
整段一次翻几十句时，靠上下文的句子（省略的主语、改口、调侃）模型顾不过来；分成小组、按顺序翻，配合高一些的思考程度，
这些句子明显更准，前后译法也更一致（按顺序翻，后面能看到前面怎么译的）。"""
from ..env import Cancelled, log, progress
from ..textutil import fmt_time

KIND = {"narration": "旁白", "dialogue": "对话", "info": "说明", "name": "人名条", "title": "标题", "sfx": "音效", "song": "歌"}
MAX_GROUP = 10


def _show(t):
    return (t or "").replace("\n", " / ")


def _mmss(t):
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


# ---------- 1. 人物表 ----------
def cast_system(src, tgt):
    return f"""你是影视字幕翻译的前期准备助手，负责{src.name}→{tgt.name}。读完一整集的原文（带时间），整理出登场的人和动物。只输出 JSON：
{{"characters": [{{"names": [{{"src": "原文里的一种叫法", "tr": "这种叫法的{tgt.name}译法"}}], "kind": "人 / 动物",
  "who": "一句话：是谁、和谁什么关系；性别只有原文能确定时才写"}}]}}
要求：
- 节目里有名字的人和动物都要收，宠物、家畜也算。同一个人 / 动物的所有叫法合成一条，每种叫法都写进 names：
  全名、只叫名字、昵称、呼唤时的音变（「ミルク」「ミル」「ミルー」「ミルや」「ミーちゃん」是同一只猫）、平假名 / 片假名不同的写法（照原文写，可带「ちゃん」）。
  names 只放名字本身，不要放「ばあちゃん」「お母さん」「妻」「先生」「店長」这类称呼。不同的人 / 动物绝不能合成一条。
- tr：每种叫法各给译法，原文怎么叫就怎么译：全名译成全名，只叫名字的译成名字，
  昵称和呼唤的音变译成和正名一样的名字，不要另起名字（观众要一眼看出是同一个：「ミーちゃん」「ミルー」都译成“牛奶”）。
  人名按{tgt.name}惯用写法，有汉字用原来的汉字；宠物名有意思的按意思译（「ミルク」→“牛奶”），没有意思的音译。
- “已有术语表”里有的名字，照术语表的译法。作品里的角色有通行译名的用通行译名。
  原文有一部分来自语音识别，人名的汉字可能听错了，和术语表、画面字、作品通行写法对不上时以那些为准。"""


def cast_input(cues, terms_text):
    lines, seen, total = [], set(), 0
    for c in cues:
        t = _show(c["orig"]["text"])
        if t in seen:
            continue
        seen.add(t)
        lines.append(f"[{_mmss(c['start'])}] {t}")
        total += len(t)
        if total > 60000:
            break
    return "## 已有术语表\n" + (terms_text or "（无）") + "\n\n## 本集原文\n" + "\n".join(lines)


def cast_terms(cast):
    """人物表里的叫法，做成术语（一个字的叫法不收，免得到处误配）。"""
    out = []
    for ch in cast:
        names = [(n.get("src") or "").strip() for n in ch.get("names") or []]
        for n in ch.get("names") or []:
            s, t = (n.get("src") or "").strip(), (n.get("tr") or "").strip()
            if len(s) >= 2 and t:
                also = "、".join(x for x in names if x and x != s)
                out.append({"src": s, "tr": t, "type": "人名",
                            "note": (ch.get("who") or "") + (f"（也叫 {also}）" if also else "")})
    return out


def _clean_cast(res):
    out = []
    for ch in (res or {}).get("characters") or []:
        if not isinstance(ch, dict):
            continue
        names = [n for n in ch.get("names") or [] if isinstance(n, dict) and n.get("src") and n.get("tr")]
        if names:
            out.append({"names": names, "kind": ch.get("kind") or "", "who": ch.get("who") or ""})
    return out


# ---------- 2. 分组 ----------
GROUP_SYSTEM = """你是纪录片字幕的编辑。给你一整集按顺序排好的字幕（只有原文），请把连续的字幕按“同一段对话 / 同一个话题”分组，翻译时一组一起翻。只输出 JSON。
- 一组是连续的几条：同一段对话（一问一答、一个人连着说的几句，包括改口、纠正、补充）、旁白讲同一件事的几句。
- 一组一般 3～8 条，最多 12 条；话题换了、场景换了、说话的人换了一批就分开。片头片尾的演职员表、标题可以单独成组。
- 每条都要分进某一组，组和组按顺序、不重叠、不留空。
输出 {"groups": [{"from": "q1", "to": "q6", "topic": "一句话：谁在说什么"}]}"""


def make_groups(res, cues):
    """模型给的分组对到字幕上：漏掉的补成一组，重叠的截掉，太大的平均拆开（每份不超过 MAX_GROUP 条）。返回 [(起, 止, 话题)]。"""
    ix = {c["id"]: i for i, c in enumerate(cues)}
    groups, pos = [], 0
    for x in (res or {}).get("groups") or []:
        if not isinstance(x, dict):
            continue
        a, b = ix.get(str(x.get("from"))), ix.get(str(x.get("to")))
        if a is None or b is None or b < pos:
            continue
        if a > pos:
            groups.append((pos, a - 1, ""))
        a = max(a, pos)
        if b < a:
            continue
        groups.append((a, b, str(x.get("topic") or "")))
        pos = b + 1
    if pos < len(cues):
        groups.append((pos, len(cues) - 1, ""))
    out = []
    for a, b, topic in groups:
        n = b - a + 1
        k = -(-n // MAX_GROUP)
        size = -(-n // k)
        for s in range(a, b + 1, size):
            out.append((s, min(b, s + size - 1), topic))
    return out


def _fallback_groups(cues):
    """分组失败时：按停顿切（停顿超过 4 秒或满 8 条）。"""
    out, a = [], 0
    for i in range(1, len(cues) + 1):
        if i == len(cues) or i - a >= 8 or cues[i]["start"] - cues[i - 1]["end"] > 4:
            out.append((a, i - 1, ""))
            a = i
    return out


# ---------- 3. 一组一组翻 ----------
def tr_system(src, tgt, context, opts):
    from .prompts import translate_rules

    return f"""你是资深{src.name}译{tgt.name}纪录片字幕译者。节目信息：{context or "（未提供）"}
每次给你一组连续的字幕（同一段对话或同一个话题），连同前后文和这几句出现时的画面截图，请把这一组的每一条翻译成{tgt.name}；前文、后文只帮助理解，不要翻译。
{translate_rules(tgt, opts)}
- 先把这一组当成一段完整的话读懂：谁在说、对谁说、省略的主语 / 宾语是谁（看画面和前后文）、语气（调侃、改口、反问、玩笑）、句和句怎么接（一个人连着说的几句要译得前后连贯）。
- 只译原文有的意思：不要加原文没有的内容，也不要漏掉（程度、时间、转折、语气词表达的意思）。原文省略的主语看不出是谁时，译文也不要硬补人称。
- 人名、地名、专有名词、动植物和食物名称按术语表统一；“本集人物”里同一行的叫法指同一个人 / 动物，原文用哪种叫法就用那种叫法后面的译名。"""


def _glossary_for(text, terms, cast):
    """只给这一组（含前后文）里出现了的术语和人物。"""
    tl = [f"{t.get('src')} → {t.get('tr')}" for t in terms if t.get("src") and t["src"] in text]
    rows = []
    for ch in cast:
        if any(n["src"] in text for n in ch["names"]):
            rows.append("、".join(f"{n['src']}→{n['tr']}" for n in ch["names"]) + f"（{ch['who']}）")
    return (("## 本集人物（同一行里的叫法指同一个人 / 动物）\n" + "\n".join(rows) + "\n\n") if rows else "") + \
        "## 术语表\n" + ("\n".join(tl) if tl else "（无）")


def _memory(group, done, tgt_code):
    from .translate import recall

    mem = recall([{"id": c["id"], "text": c["orig"]["text"]} for c in group], done, tgt_code)
    if not mem:
        return ""
    return ("## 前面出现过的相同 / 相近原文（译文记忆）\n意思和场景都相同就沿用前面的译法，保持全集一致；场景变了按这里的意思译。\n"
            + "\n".join(f"[{iid}] 前面 {_mmss(t)}：{_show(o)} → {_show(tr)}" for iid, t, o, tr, _, _ in mem) + "\n\n")


def group_prompt(cues, a, b, topic, tgt_code, summary, terms, cast, done=None):
    """done：前面已经翻好的字幕（翻译时可能合过几条，和 cues 对不上序号）；不给就用 cues[:a]。"""
    from .listen import note_text
    from .translate import listen_note

    done = cues[:a] if done is None else done
    pre, grp, post = done[-8:], cues[a:b + 1], cues[b + 1:b + 7]
    seen = "\n".join(c["orig"]["text"] for c in pre + grp + post)
    tr = lambda c: c.get("tr", {}).get(tgt_code, {}).get("text", "")
    return (f"## 本集概要\n{summary or '（无）'}\n\n{_glossary_for(seen, terms, cast)}\n\n"
            + ("## 前文（只帮助理解）\n" + "\n".join(f"{_mmss(c['start'])} {_show(c['orig']['text'])} → {_show(tr(c))}" for c in pre)
               + "\n\n" if pre else "")
            + _memory(grp, done, tgt_code)
            + f"## 这一组{f'（{topic}）' if topic else ''}\n"
            + "\n".join(f"[{c['id']}] {_mmss(c['start'])}-{_mmss(c['end'])} {KIND.get(c.get('type'), c.get('type'))}：{_show(c['orig']['text'])}"
                        f"{note_text(c)}" for c in grp)
            + ("\n\n## 后文（只帮助理解）\n" + "\n".join(f"{_mmss(c['start'])} {_show(c['orig']['text'])}" for c in post) if post else "")
            + listen_note(grp)
            + "\n\n请按要求输出 JSON。")


def run(deep, plain, cues, cfg, src, tgt, tgt_code, glossary, known_terms, frames=None):
    """分组按顺序翻整理好的字幕（只有原文），返回翻好的字幕（可能把相邻几条合成了一条；没翻到的留着空译文，之后补翻）。
    deep：翻每一组用（带思考程度）；plain：人物表、分组用。人物表的叫法并进 glossary["terms"]（作品术语表优先），
    人物表存进 glossary["cast"]。frames：{来源条目 id: 截图路径}。"""
    from . import prompts
    from .translate import Asker, apply_translation, merge_terms

    if not cues:
        return []
    log("分组翻：整理人物表（同一个人 / 动物的各种叫法）")
    try:
        cast = _clean_cast(plain.chat_json(cast_system(src, tgt), cast_input(cues, prompts.glossary_text(known_terms))))
    except Cancelled:
        raise
    except Exception as e:
        log(f"  人物表没整理出来（{str(e)[:150]}），不带人物表翻")
        cast = []
    if cast:
        log("  " + "；".join("、".join(f"{n['src']}→{n['tr']}" for n in ch["names"][:4]) for ch in cast[:12])
            + ("……" if len(cast) > 12 else ""))
        # 人物表的叫法排在这一集自动收的术语前面（同一个名字以人物表为准，最后统一术语时不会改回去）；作品术语表仍然最优先
        known_src = {t.get("src") for t in known_terms or []}
        rest = [t for t in glossary.get("terms") or [] if t.get("src") not in known_src]
        glossary["terms"] = merge_terms(known_terms, cast_terms(cast) + rest)
        glossary["cast"] = cast
    terms = glossary.get("terms") or []

    log("分组翻：按话题分组")
    try:
        groups = make_groups(plain.chat_json(GROUP_SYSTEM, "\n".join(
            f"[{c['id']}] {_mmss(c['start'])} {KIND.get(c.get('type'), c.get('type'))}：{_show(c['orig']['text'])}" for c in cues)), cues)
    except Cancelled:
        raise
    except Exception as e:
        log(f"  分组失败（{str(e)[:150]}），按停顿分组")
        groups = _fallback_groups(cues)
    log(f"  {len(groups)} 组，平均 {len(cues) / len(groups):.1f} 条"
        + (f"；思考程度「{deep.effort}」" if deep.effort else ""))

    system = tr_system(src, tgt, cfg.project.context, cfg.render.lang_options.get(tgt_code, {}))
    ask = Asker(deep, frames)
    done, failed = [], 0
    for n, (a, b, topic) in enumerate(groups):
        progress(n, len(groups), f"分组翻 {n + 1}/{len(groups)} 组", every=0)
        grp = cues[a:b + 1]
        prompt = group_prompt(cues, a, b, topic, tgt_code, glossary.get("summary"), terms, cast, done)
        pics = [c for c in grp if c.get("type") in ("dialogue", "narration")]
        try:
            out, missing = apply_translation(grp, ask(system, prompt, pics, limit=12), tgt_code)
            if missing:
                retry = prompt + f"\n\n上次输出漏掉了这些 id：{', '.join(c['id'] for c in missing)}。请重新完整输出这一组。"
                out2, missing2 = apply_translation(grp, ask(system, retry, pics, limit=12), tgt_code)
                if len(missing2) < len(missing):
                    out, missing = out2, missing2
        except Cancelled:
            raise
        except Exception as e:
            log(f"  第 {n + 1} 组（{fmt_time(grp[0]['start'])} 起）没翻成，之后逐条补翻：{str(e)[:150]}")
            out, missing = [], grp
        failed += len(missing)
        done += sorted(out + [dict(c, flags=list(c.get("flags", []))) for c in missing], key=lambda c: (c["start"], c["end"]))
    progress(len(groups), len(groups), "分组翻", every=0)
    log(f"分组翻完成：{len(groups)} 组" + (f"，{failed} 条没翻到、之后逐条补翻" if failed else ""))
    return done
