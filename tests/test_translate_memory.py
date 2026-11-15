"""译文记忆：翻到和前面相同 / 很像的原文时，把前面的译法和前后句给大模型参考（沿用还是另译由它判断）。"""
from nyasub.llm.prompts import chunk_prompt, glossary_system, translate_system
from nyasub.llm.translate import recall


def _cue(start, orig, tr):
    return {"start": start, "end": start + 2, "orig": {"lang": "ja", "text": orig}, "tr": {"zh-Hans": {"text": tr}}}


def test_recall_finds_same_and_similar_lines_with_context():
    done = [_cue(10, "鴨が飛んできた", "鸭子飞来了"), _cue(12, "（鳴き声）", "（鸭鸣）"), _cue(14, "冬を食べる", "品尝冬日"),
            _cue(50, "うん。", "嗯"), _cue(80, "冬を食べる", "品味冬天"), _cue(90, "最初に食べた人は｜勇気のある方だったのかな", "最先吃的人｜一定很有勇气吧")]
    chunk = [{"id": "o9", "text": "冬を食べる"}, {"id": "c3", "text": "（鳴き声）"}, {"id": "c4", "text": "うん"},
             {"id": "c5", "text": "最初に食べた人は\n勇気のある方だったのかなあ"}, {"id": "c6", "text": "まったく別の話"}]
    mem = recall(chunk, done, "zh-Hans")
    got = {(m[0], m[3]) for m in mem}
    # 前面两种译法都给，由大模型判断；带前后句
    assert ("o9", "品尝冬日") in got and ("o9", "品味冬天") in got
    first = next(m for m in mem if m[3] == "品尝冬日")
    assert first[1] == 14 and first[4] == "（鳴き声）" and first[5] == "うん。"
    assert ("c3", "（鸭鸣）") in got                      # 同样的音效：给参考，不强行统一
    assert all(m[0] != "c4" for m in mem)                 # “うん”太短，不找
    assert ("c5", "最先吃的人｜一定很有勇气吧") in got     # 差一个字也算很像
    assert all(m[0] != "c6" for m in mem)


def test_memory_shown_in_prompt_as_reference():
    mem = [("o9", 14.0, "冬を食べる", "品尝冬日", "（鳴き声）", "うん。")]
    p = chunk_prompt([], "（无）", "", [], "zh-Hans", mem)
    assert "译文记忆" in p and "[o9] 前面 00:14" in p and "品尝冬日" in p and "场景变了" in p
    assert "译文记忆" not in chunk_prompt([], "（无）", "", [], "zh-Hans")


def test_glossary_text_gives_user_notes_only():
    """人工写的备注（手动加的、导入的）给翻译：说话习惯这种光看字幕猜不到（村雨用「村雨」称呼自己）；自动整理的备注不给。"""
    from nyasub.llm import prompts

    text = prompts.glossary_text([
        {"src": "村雨", "reading": "むらさめ", "tr": "村雨", "note": "说话时用「村雨」称呼自己", "from": "import"},
        {"src": "健太", "tr": "健太", "note": "", "from": "manual"},
        {"src": "大塚村", "tr": "大冢村", "note": "信乃的故乡", "from": "ep01"}])
    assert text.splitlines() == ["村雨（むらさめ） → 村雨　备注：说话时用「村雨」称呼自己", "健太 → 健太", "大塚村 → 大冢村"]


def test_episode_synopsis_feeds_summary_generation():
    from nyasub.lang import get

    prompt = glossary_system(get("ja"), get("zh-Hans"), "纪录片背景", "本集介绍传统食物")
    assert "纪录片背景" in prompt and "本集介绍传统食物" in prompt


def test_translation_uses_generated_summary_as_background():
    from nyasub.lang import get

    prompt = translate_system(get("ja"), get("zh-Hans"), "本集概要：节目介绍传统食物", {})
    assert "本集概要：节目介绍传统食物" in prompt
