import re

from .base import LanguagePack


class Japanese(LanguagePack):
    code = "ja"
    name = "日语"
    whisper = "ja"
    ocr_lang = "japan"
    line_chars = 30
    cps = 8
    hallucinations = ("ご視聴ありがとうございました", "チャンネル登録", "最後までご覧いただき", "字幕は")
    name_super = re.compile(r"[（(]\s*\d{1,3}\s*(?:歳)?\s*[)）]\s*$|(?:さん|氏|くん|ちゃん)\s*$")
    meaningful = re.compile(r"[぀-ヿ㐀-鿿]")
    break_after = set("　 、。，；：？！…」』）】》”")
    no_line_start = set("、。，．・：；？！ー」』）】》ぁぃぅぇぉっゃゅょァィゥェォッャュョ")

    def prompt_source_notes(self) -> str:
        return ("日文字幕习惯：外挂字幕里的（　）通常是说话人或音效，≪≫ 表示画外音，♪ 表示歌曲；"
                "方言台词按意思理解。")

    def display(self, text, opts):
        lines = [re.sub(r"[。、\s　]+$", "", l.strip()) for l in (text or "").replace("\\N", "\n").split("\n")]
        return "　".join(l for l in lines if l)
