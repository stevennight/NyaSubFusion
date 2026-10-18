"""提示词：按“源语言 → 目标语言”生成，语言相关的要求来自语言包。"""
from ..textutil import fmt_time

SRC_LABEL = {"subs": "字幕", "telop": "画面字", "info": "画面字", "asr": "语音识别"}


def glossary_system(src, tgt) -> str:
    return f"""你是影视字幕翻译的术语整理助手，负责{src.name}→{tgt.name}。
给你一集节目里出现的全部{src.name}文本（字幕 + 画面字 OCR，可能有 OCR 错字）。
请输出 JSON：
{{"summary": "用中文 3~6 句话概括本集内容（地点、人物、主题）",
 "terms": [{{"src": "原文写法", "reading": "读音（知道就填）", "tr": "{tgt.name}译法", "type": "地名/人名/团体/物品/方言/其他", "note": "可选"}}]}}
要求：
- 只收专有名词、有地方特色的事物、反复出现的方言词或口头禅，最多 150 条，按重要程度排序。
- 人名、地名按{tgt.name}的惯用译法；没有惯用译法的给出合适的音译。
- OCR 错字明显时按正确写法收录。
- 如果提供了“已有术语表”，其中的条目不要重复输出，只补充新的。"""


def chunk_system(src, tgt, context: str, tgt_opts: dict) -> str:
    notes = src.prompt_source_notes()
    return f"""你是资深{src.name}→{tgt.name}影视字幕译者兼校对。节目信息：{context or "（未提供）"}
你会收到一段时间窗口内的全部“候选字幕条目”，来自三个来源：
- 字幕（subs）：视频自带的字幕，文字最可信。注意官方字幕常会省略画面上已经有字的台词。
- 画面字：从画面 OCR 识别的硬字幕，可能有错字、漏字。后面标了位置和“像台词 / 像说明”，那只是粗略猜测，以内容为准：
  说话内容（口语、带语气、是某人说的话）就是台词 dialogue，哪怕它在画面偏左偏右；地名、人名、说明、标题、资料来源才是 info。
- 语音识别（asr）：只列出没有任何字幕覆盖的语音，文字不可靠（尤其方言）。
  另外，部分条目后面附有「语音参考」，是同一时段语音识别听到的内容，常有听错，只用来帮助纠正 OCR 错字、理解句意，不要当成另一句话。
{notes}
你的任务：把这些条目整理成最终字幕，并翻译成{tgt.name}。输出 JSON：
{{"cues": [{{"src": ["c12", "o5"], "type": "dialogue|narration|info|song|sfx", "orig": "整理后的{src.name}原文", "tr": "{tgt.name}译文"}}],
  "drop": ["a3", "o9"]}}

规则：
1. 每个输入 id 必须且只能出现一次：要么在某条 cue 的 src 里，要么在 drop 里。
2. 同一句话被拆在字幕和画面字里（半句在这、半句在那），或者两边内容重复时，合并成一条 cue，src 列出全部来源 id；只合并时间上相邻（间隔不超过约 2 秒）的条目。
   画面上的标题、菜名、人名条、地名这类说明文字，如果没有人把它说出来（字幕和语音里都没有这几个字），
   就算内容相关、时间重叠，也绝不能和字幕 / 语音的台词合成一条：它单独成一条 type 为 info 的 cue。
3. cue 按时间顺序输出。不要自己编时间，时间由 src 决定。
4. OCR 错字结合上下文、语音参考和术语表纠正后写进 orig。
5. 语音识别条目：确实有意义、而且画面和字幕里都没有的台词，单独成一条 cue；与前后字幕重复、听错、噪音、无意义的，放进 drop。
6. 画面文字只保留节目后期加上的字幕：采访台词、人名条、地名、说明文字、标题、资料来源。
   画面里实物上本来就有的字一律放进 drop：地图和古文书上的字、招牌、海报、日历、商品包装和标签、价格牌、手写字、台标。
   拿不准时宁可 drop。绝不能输出“地图文字”“画面文字”之类的占位译文。
7. 音效、画外音、歌曲按{tgt.name}字幕习惯表达，例如音效写在括号里，歌曲前加 ♪。
8. 人名、地名、专有名词按术语表统一。
9. {tgt.prompt_target_rules(tgt_opts)}"""


def _position(it):
    zone = it.get("zone")
    if zone in ("left", "right"):
        return {"left": "左侧竖排", "right": "右侧竖排"}[zone]
    r = it.get("rect")
    v = {"bottom": "底部", "top": "顶部", "middle": "中部"}.get(zone, "")
    if not r:
        return v
    cx = (r[0] + r[2]) / 2
    h = "居中" if abs(cx - 0.5) < 0.12 else ("偏左" if cx < 0.5 else "偏右")
    return v + h


def item_line(it) -> str:
    label = SRC_LABEL[it["src"]]
    extra = []
    if it["src"] in ("telop", "info"):
        # 不替模型下结论：只给位置和倾向，台词还是说明由它按内容判断
        extra.append(_position(it))
        extra.append("像台词" if it["src"] == "telop" else "像说明")
        if it.get("over_speech"):
            extra.append("同一时间字幕在说别的话")
        if it.get("ocr_score", 1) < 0.85:
            extra.append("OCR 置信度低")
    if it["src"] == "asr":
        extra.append("无字幕覆盖")
        if it.get("second_pass"):
            extra.append("补识别：可能是哭喊、歌声，也可能是噪音或幻觉")
    extra = [e for e in extra if e]
    tag = label + (f"（{'，'.join(extra)}）" if extra else "")
    text = it["text"].replace("\n", " / ")
    hint = f"　〔语音参考：{it['asr_hint']}〕" if it.get("asr_hint") else ""
    return f"[{it['id']}] {fmt_time(it['start'])}-{fmt_time(it['end'])} {tag}：{text}{hint}"


def glossary_text(terms) -> str:
    if not terms:
        return "（无）"
    lines = []
    for t in terms[:200]:
        r = f"（{t['reading']}）" if t.get("reading") else ""
        lines.append(f"{t.get('src', '')}{r} → {t.get('tr', '')}")
    return "\n".join(lines)


def glossary_input(items, known_terms) -> str:
    seen, lines, total = set(), [], 0
    for it in items:
        if it["src"] == "asr":
            continue
        t = it["text"].replace("\n", " ")
        if t in seen:
            continue
        seen.add(t)
        lines.append(t)
        total += len(t)
        if total > 40000:
            break
    head = f"## 已有术语表\n{glossary_text(known_terms)}\n\n## 本集文本\n" if known_terms else ""
    return head + "\n".join(lines)


def chunk_prompt(chunk, gl_text, summary, context_cues, tgt_code) -> str:
    parts = []
    if summary:
        parts.append(f"## 本集概要\n{summary}")
    parts.append(f"## 术语表\n{gl_text}")
    if context_cues:
        parts.append("## 前文（已翻译，仅供衔接参考，不要输出）\n" + "\n".join(
            f"{c['orig']['text'].replace(chr(10), ' / ')} → {c['tr'].get(tgt_code, {}).get('text', '')}"
            for c in context_cues))
    parts.append("## 本段候选条目\n" + "\n".join(item_line(it) for it in chunk))
    parts.append("请按规则输出 JSON。")
    return "\n\n".join(parts)


def extra_target_system(src, tgt, ref, context: str, tgt_opts: dict) -> str:
    return f"""你是资深{src.name}→{tgt.name}影视字幕译者。节目信息：{context or "（未提供）"}
你会收到已经整理好的{src.name}字幕，每条带 id，并附有{ref.name}参考译文（只帮助理解，不要照搬它的措辞）。
请把每条翻译成{tgt.name}，输出 JSON：{{"items": [{{"id": "q12", "tr": "译文"}}]}}，每个 id 都要有且只有一条。
人名、地名、专有名词按术语表统一。{tgt.prompt_target_rules(tgt_opts)}
原文里的“｜”是分段标记，译文要分成同样多段。"""


def extra_target_prompt(cues, gl_text, src_code, ref_code) -> str:
    lines = [f"[{c['id']}] {c['orig']['text'].replace(chr(10), ' / ')}　〔参考：{c['tr'].get(ref_code, {}).get('text', '')}〕"
             for c in cues]
    return f"## 术语表\n{gl_text}\n\n## 字幕\n" + "\n".join(lines) + "\n\n请按要求输出 JSON。"
