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
