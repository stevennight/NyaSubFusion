"""画面字跟踪：滚动字幕（跑马灯、片尾字幕）每条单独成句，不和背景里的字拼成一大条。"""
from nyasub.sources import ocr

W, H = 1280, 720
C = {"min_duration": 0.6, "max_gap": 0.75, "persistent_seconds": 45.0}
FPS = 6
CH = 36  # 字宽 / 字高（像素）


def _track(frames):
    """frames: [(t, [(text, rect)])] → 跟踪完的轨迹（和 scan 里的循环一样，只是不截图）。"""
    active, closed, nid = [], [], 1
    for t, dets in frames:
        dets = [{"text": x, "rect": r, "score": 0.95, "vertical": False} for x, r in dets]
        for d, tr in ocr._match(active, dets, t):
            if tr is None:
                active.append(ocr.Track(nid, t, d))
                nid += 1
            else:
                tr.add(t, d)
                n = len(d["text"])
                if n > tr.snap_len:
                    tr.snap_len, tr.snap_rect, tr.snap_t, tr.snapshot = n, d["rect"], t, "x"
        still = []
        for tr in active:
            (still if t - tr.last <= C["max_gap"] else closed).append(tr)
        active = still
    return closed + active


def _crawl(items, speed, y, seconds, also=()):
    """一行字从右往左跑：items 是 [(文字, 起点 x)]，出画面的部分截掉（文字也只剩看得见的那几个字）。"""
    frames = []
    for k in range(int(seconds * FPS)):
        t = k / FPS
        dets = list(also)
        for text, x0 in items:
            x = x0 - speed * t
            lo, hi = max(0.0, x), min(float(W), x + CH * len(text))
            if hi - lo < CH:
                continue
            a, b = int((lo - x) // CH), int(-(-(hi - x) // CH))
            dets.append((text[a:b], (lo, y, hi, y + CH)))
        frames.append((t, dets))
    return frames


def test_crawl_items_tracked_one_event_each():
    items = [("語り柴田祐規子", 1300), ("撮影岡本亮", 1700), ("音声濱口大蔵", 2050), ("制作統括河合明子", 2450)]
    # 背景里一直不动的字（招牌），不能和滚动字幕拼到一起
    sign = ("雪菜の里", (900.0, 560.0, 1040.0, 596.0))
    ev = ocr._build_events(_track(_crawl(items, 300, 600, 14, also=[sign])), C, FPS, W, H, 3600)
    texts = [e["text"] for e in ev]
    for text, _ in items:
        assert texts.count(text) == 1, texts
    assert "雪菜の里" in texts
    moving = [e for e in ev if e.get("moving")]
    assert sorted(e["text"] for e in moving) == sorted(t for t, _ in items)
    assert all("moving" not in e for e in ev if e["text"] == "雪菜の里")


def _roll(lines, speed, seconds):
    """片尾字幕往上滚：lines 是 [(职务, 姓名, 起点 y)]，职务和姓名识别成两个框，中间空一个字。"""
    frames = []
    for k in range(int(seconds * FPS)):
        t = k / FPS
        dets = []
        for role, name, y0 in lines:
            y = y0 - speed * t
            if 0 <= y and y + CH <= H:
                x = 400.0
                dets.append((role, (x, y, x + CH * len(role), y + CH)))
                x += CH * (len(role) + 1)
                dets.append((name, (x, y, x + CH * len(name), y + CH)))
        frames.append((t, dets))
    return frames


def test_roll_role_and_name_joined_line_by_line():
    lines = [("語り", "松たか子", 730), ("撮影", "山田太郎", 730 + 2 * CH), ("音響効果", "高橋次郎", 730 + 4 * CH)]
    ev = ocr._build_events(_track(_roll(lines, 80, 14)), C, FPS, W, H, 3600)
    assert sorted(e["text"] for e in ev) == ["撮影山田太郎", "語り松たか子", "音響効果高橋次郎"]


def test_crawl_pieces_along_motion_kept_apart():
    # 横着走的字：前后两段间隔一个字也不拼（一条条片尾字幕 / 跑马灯的几句话本来就挨着）
    items = [("語り", 1300), ("松たか子", 1300 + 3 * CH), ("撮影", 1300 + 8 * CH)]
    ev = ocr._build_events(_track(_crawl(items, 300, 600, 12)), C, FPS, W, H, 3600)
    assert sorted(e["text"] for e in ev) == ["撮影", "松たか子", "語り"]


def test_two_row_crawl_joins_rows_of_one_credit():
    # 横着走的两行片尾字幕：上行「职务 姓名」、下行第二个人，同一条的上下两行拼成一条
    top = _crawl([("撮影岡本亮", 1300), ("音声濱口大蔵", 1300 + 9 * CH)], 300, 600, 12)
    bottom = _crawl([("水上智重子", 1300 + 2 * CH), ("佐久間敏美", 1300 + 12 * CH)], 300, 642, 12)
    frames = [(t, a + b) for (t, a), (_, b) in zip(top, bottom)]
    ev = ocr._build_events(_track(frames), C, FPS, W, H, 3600)
    assert sorted(e["text"] for e in ev) == ["撮影岡本亮\n水上智重子", "音声濱口大蔵\n佐久間敏美"]


def test_fast_crawl_still_tracked_with_prediction():
    # 每帧移动 60 像素、比一个两字的词还宽一半：只看上一帧的位置跟不上
    ev = ocr._build_events(_track(_crawl([("語り", 1300), ("井上二郎", 1500)], 360, 640, 6)), C, FPS, W, H, 3600)
    assert sorted(e["text"] for e in ev) == ["井上二郎", "語り"]


def test_long_crawl_not_dropped_as_logo():
    # 很慢的跑马灯：一条要在画面上待一分多钟，不能当成台标去掉
    ev = ocr._build_events(_track(_crawl([("きょうの天気は晴れ", 1300)], 18, 650, 80)), C, FPS, W, H, 600)
    assert [e["text"] for e in ev] == ["きょうの天気は晴れ"]


def test_static_lines_still_grouped():
    two = [("北海道", (500.0, 600.0, 608.0, 636.0)), ("積丹町", (500.0, 642.0, 608.0, 678.0))]
    frames = [(k / FPS, two) for k in range(18)]
    ev = ocr._build_events(_track(frames), C, FPS, W, H, 3600)
    assert [e["text"] for e in ev] == ["北海道\n積丹町"]
    assert "moving" not in ev[0]
    assert [p[4] for p in ev[0]["pieces"]] == ["北海道", "積丹町"]  # 每块字各自的框也记下来


def test_tight_rect_keeps_only_caption_lines():
    # 招牌上的字（わに / わにで / かんす）被并进了字幕这条画面字；最终原文里只有字幕两行，框只包这两行
    pieces = [[0.627, 0.686, 0.753, 0.826, "わに"], [0.788, 0.653, 0.905, 0.761, "わにで"],
              [0.221, 0.835, 0.536, 0.91, "(ワニが)ほんと好き"], [0.22, 0.899, 0.781, 0.988, "お正月くらいしか食べられないから"],
              [0.765, 0.89, 0.888, 0.982, "かんす"]]
    text = "わにわにで\n(ワニが)ほんと好き油\nお正月くらいしか食べられないからかんす"
    assert ocr.tight_rect(pieces, text, "（ワニが）ほんと好き　お正月くらいしか食べられないから") == [0.22, 0.835, 0.781, 0.988]
    # 注音小字、漏翻的一行（对齐、紧挨着）画面上照样有，留在框里
    ruby = [[0.3836, 0.7958, 0.457, 0.8417, "そじょう"], [0.2898, 0.8292, 0.7094, 0.9222, "鮭が遡上している時期は"],
            [0.2945, 0.9042, 0.568, 0.9917, "きっとにぎやかで"]]
    assert ocr.tight_rect(ruby, "そじょう\n鮭が遡上している時期は\nきっとにぎやかで", "鮭が遡上している時期は　きっとにぎやかで")[1] == 0.7958
    lines = [[0.2469, 0.7986, 0.7531, 0.9069, "(1キロ)4,000円とか5,000円"], [0.2523, 0.8944, 0.6047, 0.9931, "120万円くらいの儲け"]]
    assert ocr.tight_rect(lines, "(1キロ)4,000円とか5,000円\n120万円くらいの儲け", "（１キロ）4,000円とか5,000円")[3] == 0.9931
    # 重新检测漏了真字幕的一行：凑不齐，不收紧
    assert ocr.tight_rect(pieces[2:3], text, "（ワニが）ほんと好き　お正月くらいしか食べられないから") is None


# ---------------- 同一处换成另一句 ----------------
# 两句结尾一样（「即いいって言ってくれたんで」→「応援するって言ってくれたんで」）也要拆成两条，各有自己的时间和截图；
# 渐入时的半截、认错一两帧、一直认错一个字都不拆；拆开后发现其实是同一句（认错了一阵）再并回去。

LINE_RECT = (500.0, 960.0, 1400.0, 1050.0)


def _lines_run(texts, fps=3.0, need=2):
    """按顺序每帧认出一句（None = 这一帧没认出字），返回轨迹。截图路径记成“帧号”，看截图取自哪一帧。"""
    active, nid = [], 1
    for i, txt in enumerate(texts):
        dets = [] if txt is None else [{"rect": LINE_RECT, "text": txt, "score": 0.99, "vertical": False}]
        nid = ocr._step(active, dets, i / fps, nid, need, lambda i=i: f"f{i}")
    return ocr._rejoin(active)


def _lines(tracks):
    return [(t.best_text(), round(t.first, 2), round(t.last, 2)) for t in tracks]


A = "即 いいって言ってくれたんで"
B = "応援するって言ってくれたんで"


def test_two_lines_same_place_are_split():
    tracks = _lines_run([A] * 6 + [B] * 6)
    assert _lines(tracks) == [(A, 0.0, 1.67), (B, 2.0, 3.67)]
    a, b = tracks
    assert int(a.snapshot[1:]) < 6 <= int(b.snapshot[1:])   # 各用自己那句的截图
    assert a.snap_t < 2.0 <= b.snap_t


def test_split_needs_the_new_line_to_stay():
    # 换上来的另一句只出现一帧就没了（闪一下 / 认错）：不拆
    assert _lines(_lines_run([A] * 5 + [B] + [A] * 5)) == [(A, 0.0, 3.33)]
    # 每秒 10 帧时要稳定半秒（5 帧）才拆
    assert len(_lines_run([A] * 10 + [B] * 4 + [A] * 3, fps=10, need=5)) == 1
    assert len(_lines_run([A] * 10 + [B] * 6, fps=10, need=5)) == 2


def test_fade_in_and_misreads_stay_one_line():
    fade = ["応援", "応援する", "応援するって言", B, B, B, B]
    assert _lines(_lines_run(fade)) == [(B, 0.0, 2.0)]
    typo = B[:-1] + "て"   # 一直认错一个字
    assert len(_lines_run([B, typo, typo, B, typo, typo, typo])) == 1


def test_misread_for_a_while_is_joined_back():
    """动画标题有半秒多都认成了别的字，后来又认对了：拆开的两条最后认定的字一样，并回一条。"""
    title, wrong = "サヨナラまでの日々", "サヨナうまでのロ々"
    assert _lines(_lines_run([title] * 4 + [wrong] * 3 + [title] * 6)) == [(title, 0.0, 4.0)]


def test_words_added_in_front_after_a_while_is_another_line():
    """「寒いです」显示了一阵，接着同一处换成「めちゃめちゃ寒いです」（又说了一句）：拆成两条。
    开头几个字只有一两帧没认出来（「ちゃ寒いです」）的不拆。"""
    short, full = "寒いです", "めちゃめちゃ寒いです"
    assert _lines(_lines_run([short] * 6 + [full] * 6, fps=10, need=5)) == [(short, 0.0, 0.5), (full, 0.6, 1.1)]
    assert len(_lines_run(["ちゃ寒いです"] * 2 + [full] * 8, fps=10, need=5)) == 1
    # 从前往后逐字出现（半截是开头）照旧是一条，哪怕半截停了一阵
    assert len(_lines_run(["めちゃめちゃ"] * 6 + [full] * 6, fps=10, need=5)) == 1


def test_moving_text_box_leaves_out_pieces_already_off_screen():
    """镜头扫过酒单：截图那一刻已经移出画面的块被夹在画面边上（[0, 1, 0, 1]），框不能因此撑到画面底下；字照旧留着。"""
    e = {"moving": True, "vertical": False, "text": "①七贤\n⑨神開", "rect": [0.0, 0.12, 0.98, 1.0],
         "crop_rect": [0.0, 0.12, 0.98, 1.0], "snap_rect": [0.0, 0.12, 0.98, 1.0], "zone": "middle",
         "pieces": [[0.70, 0.12, 0.74, 0.16, "①七贤"], [0.06, 0.50, 0.98, 0.65, "③八海山"], [0.0, 1.0, 0.0, 1.0, "⑨神開"]]}
    ocr.drop_offscreen(e)
    assert e["rect"] == e["crop_rect"] == e["snap_rect"] == [0.06, 0.12, 0.98, 0.65]
    assert len(e["pieces"]) == 2 and e["text"] == "①七贤\n⑨神開"
    still = {"vertical": False, "rect": [0.0, 0.5, 0.1, 1.0], "pieces": [[0.0, 1.0, 0.0, 1.0, "x"], [0.0, 0.5, 0.1, 0.6, "y"]]}
    assert ocr.drop_offscreen(dict(still))["rect"] == still["rect"]  # 不是移动的字：不动
