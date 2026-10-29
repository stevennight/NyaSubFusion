"""画面字里的振假名：并到下面那条画面字上，去掉视觉复核写进来的注音行，分词读错的读音交给注音。"""
from nyasub import lang
from nyasub.fusion.fuse import prefilter
from nyasub.sources.ocr import attach_ruby

JA = lang.get("ja")


def ev(id, text, rect, start=10.0, end=13.0, **kw):
    return {"id": id, "start": start, "end": end, "text": text, "rect": list(rect), "vertical": False,
            "lines": len(text.split("\n")), "zone": "bottom", "score": 0.99, **kw}


def test_furigana_attaches_to_kanji_below():
    """「瞬」上面的「まばた」：并到下面那条，框包住注音；分词本来就读对了，不用给读音参考。"""
    ruby = ev("o1", "まばた", (0.298, 0.812, 0.340, 0.843))
    base = ev("o2", "瞬きひとつのタイミングが\n早いか遅いか", (0.298, 0.835, 0.704, 0.981))
    out = {e["id"]: e for e in attach_ruby([ruby, base], JA)}
    assert out["o1"]["ruby_of"] == "o2"
    assert out["o2"]["ruby"] == [{"base": "瞬", "reading": "まばた", "hint": None}]
    assert out["o2"]["rect"][1] == 0.812 and out["o2"]["text"] == base["text"]
    assert "ruby_of" not in ruby and "ruby" not in base  # 不改传进来的
    prefilter(list(out.values()), 60, JA)
    assert out["o1"]["dropped"] == "ruby" and out["o1"]["prefiltered"]  # 融合时丢掉，不送去复核


def test_vlm_furigana_line_removed_and_truncation_restored():
    """复核把注音写成了单独一行（「もう / 儲からない仕事」），还漏了第二行：去掉注音行，换回本地识别的完整两行。"""
    ruby = ev("o1", "もう", (0.290, 0.800, 0.330, 0.832))
    base = ev("o2", "もう\n儲からない仕事", (0.246, 0.832, 0.752, 0.982),
              text_local="儲からない仕事\nどうして若者がやりたがります")
    out = {e["id"]: e for e in attach_ruby([ruby, base], JA)}
    assert out["o2"]["text"] == "儲からない仕事\nどうして若者がやりたがります" and out["o2"]["lines"] == 2
    assert out["o2"]["ruby"][0]["base"] == "儲"
    # 没漏行的只去掉注音行
    base2 = ev("o2", "わらづと\n藁苞", (0.673, 0.824, 0.799, 0.948), text_local="藁苞")
    ruby2 = ev("o1", "わらづと", (0.680, 0.790, 0.790, 0.822))
    out = {e["id"]: e for e in attach_ruby([ruby2, base2], JA)}
    assert out["o2"]["text"] == "藁苞" and out["o2"]["ruby"][0]["base"] == "藁苞"


def test_not_furigana():
    """不是注音的不动：下面没有汉字的（手写字旁边的假名）、和下面的字一样大的、隔得远的、时间对不上的、说明以外的语言。"""
    base = ev("o2", "瞬きひとつのタイミングが", (0.298, 0.835, 0.704, 0.905))
    cases = [
        [ev("o1", "まばた", (0.298, 0.812, 0.340, 0.843)), ev("o2", "おとうさん　おかあさん", (0.298, 0.835, 0.704, 0.905))],
        [ev("o1", "まばたき", (0.298, 0.770, 0.420, 0.835)), base],      # 一样大：另一行字
        [ev("o1", "まばた", (0.298, 0.700, 0.340, 0.731)), base],        # 隔得远
        [ev("o1", "まばた", (0.298, 0.812, 0.340, 0.843), start=20, end=22), base],
    ]
    for events in cases:
        assert not any(e.get("ruby_of") for e in attach_ruby(events, JA))
    assert not any(e.get("ruby_of") for e in attach_ruby(cases[0][:1] + [base], lang.get("zh-Hans")))


def test_vertical_furigana_on_the_right():
    """竖排：注音在那一列的右边。"""
    base = dict(ev("o2", "贅沢な食べ物", (0.50, 0.20, 0.55, 0.60)), vertical=True)
    ruby = dict(ev("o1", "ぜいたく", (0.552, 0.20, 0.570, 0.33)), vertical=True)
    out = {e["id"]: e for e in attach_ruby([ruby, base], JA)}
    assert out["o1"]["ruby_of"] == "o2" and out["o2"]["ruby"][0]["base"] == "贅沢"


def test_reading_hint_only_when_dictionary_disagrees():
    """分词读错的（積丹 → せきたん 这种）才交给注音；读对了的不用管；振假名带上了送假名也认。"""
    assert JA.furigana_hint("積丹の海", 0, 2, "しゃこたん") is None
    assert JA.furigana_hint("積丹の海", 0, 2, "せきたん") == {"src": "積丹", "reading": "せきたん"}
    assert JA.furigana_hint("瞬きひとつ", 0, 1, "まばたき") is None
    assert JA.furigana_hint("瞬きひとつ", 0, 1, "まばた") is None
