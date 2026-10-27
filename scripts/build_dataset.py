#!/usr/bin/env python3
"""Merge OpenJLPT and the local JLPT word lists into static app data."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = APP_DIR / "data"
OPEN_DIR = DATA_DIR / "source" / "openjlpt" / "vocab"
LOCAL_DIR = DATA_DIR / "source" / "local"
PDF_JSON = LOCAL_DIR / "pdf" / "pdf_vocab.json"
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
PDF_DIR = WORKSPACE_ROOT / "resources" / "jlpt_lists"
LEVELS = ["N5", "N4", "N3", "N2", "N1"]
RANK = {level: index for index, level in enumerate(LEVELS)}
KANJI_RE = re.compile(r"[\u3005\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
KANA_RE = re.compile(r"^[\u3040-\u309f\u30a0-\u30ff\u3005\u30fc\s()（）・,，]+$")
FURIGANA_RE = re.compile(r"\{([^{}|]+)\|([^{}|]+)\}")

sys.path.insert(0, str(APP_DIR / ".build-tools"))
try:
    import fitz
    from pykakasi import kakasi
except ImportError as exc:
    raise SystemExit(
        "Install build dependencies with: python -m pip install -r requirements-build.txt"
    ) from exc

KAKASI = kakasi()


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "").strip())
    return value.replace("〜", "～").replace("〜", "～")


def key_for(word: str, reading: str) -> tuple[str, str]:
    return normalize(word), normalize(reading)


def has_kanji(value: str) -> bool:
    return bool(KANJI_RE.search(value))


def hiragana(value: str) -> str:
    result = []
    for char in value:
        code = ord(char)
        result.append(chr(code - 0x60) if 0x30A1 <= code <= 0x30F6 else char)
    return "".join(result)


def annotate_reading(value: str, reading: str) -> str:
    """Align kana anchors so okurigana stays outside the ruby base."""
    reading = hiragana(reading)
    out = []
    cursor = 0
    plain = []
    def flush_plain():
        if plain:
            out.append("".join(plain))
            plain.clear()
    i = 0
    while i < len(value):
        if not KANJI_RE.fullmatch(value[i]):
            flush_plain()
            out.append(value[i])
            anchor_char = hiragana(value[i])
            if anchor_char in reading[cursor:]:
                cursor = reading.find(anchor_char, cursor) + len(anchor_char)
            i += 1
            continue
        start = i
        while i < len(value) and KANJI_RE.fullmatch(value[i]):
            i += 1
        base = value[start:i]
        next_kana = next((ch for ch in value[i:] if '\u3040' <= ch <= '\u309f'), None)
        if next_kana:
            search = reading.find if KANJI_RE.search(value[i:]) else reading.rfind
            anchor = search(next_kana, cursor)
            if anchor == cursor:
                later = reading.find(next_kana, cursor + 1)
                if later >= 0:
                    anchor = later
            if anchor < 0:
                anchor = cursor
            ruby = reading[cursor:anchor]
        else:
            ruby = reading[cursor:]
            anchor = len(reading)
        if ruby:
            flush_plain()
            out.append("{" + base + "|" + ruby + "}")
        else:
            plain.append(base)
        cursor = anchor
    flush_plain()
    return "".join(out)


def ruby_markup(value: str) -> str:
    """Use dictionary segment readings and keep kana suffixes outside ruby."""
    parts = []
    for piece in KAKASI.convert(value):
        if has_kanji(piece["orig"]):
            parts.append(annotate_reading(piece["orig"], piece["hira"]))
        else:
            parts.append(piece["orig"])
    return "".join(parts) or value


def strip_ruby(value: str) -> str:
    return FURIGANA_RE.sub(lambda match: match.group(1), value)


def all_kanji_annotated(value: str) -> bool:
    visible = strip_ruby(value)
    return [c for c in value if KANJI_RE.fullmatch(c)] == [c for c in visible if KANJI_RE.fullmatch(c)] and not has_kanji(
        FURIGANA_RE.sub("", value)
    )


def word_markup(word: str, reading: str) -> str:
    if not has_kanji(word):
        return word
    word_parts = re.split(r"([/;])", word)
    reading_parts = re.split(r"\s*[/;]\s*", reading)
    lexical_parts = [part for part in word_parts if part not in {"/", ";"}]
    if len(lexical_parts) == len(reading_parts) and len(lexical_parts) > 1:
        out = []
        reading_index = 0
        for part in word_parts:
            if part in {"/", ";"}:
                out.append(part)
            else:
                clean = re.sub(r"\s*[\uFF08(][^()\uFF08\uFF09]*[\uFF09)]\s*", "", reading_parts[reading_index])
                clean = clean.split()[0] if clean.split() else clean
                clean = re.sub(r"[~\u301C\uFF5E]", "", clean)
                out.append(annotate_reading(part, clean))
                reading_index += 1
        return "".join(out)
    selected_reading = reading_parts[0] if reading_parts else reading
    selected_reading = re.sub(r"\s*[\uFF08(][^()\uFF08\uFF09]*[\uFF09)]\s*", "", selected_reading)
    selected_reading = selected_reading.split()[0] if selected_reading.split() else selected_reading
    selected_reading = re.sub(r"[~\u301C\uFF5E]", "", selected_reading)
    candidate = annotate_reading(word, selected_reading)
    return candidate if all_kanji_annotated(candidate) else ruby_markup(word)


def page_word_lines(page_text: str) -> list[str]:
    lines = []
    for raw in page_text.splitlines():
        line = raw.strip()
        if not line or line.isdigit():
            continue
        if "tanos.co.uk/jlpt" in line or line.lower() in {"kanji", "hiragana", "english"}:
            continue
        if line.startswith("JLPT N") or line.startswith("This list is not"):
            continue
        lines.append(line)
    return lines


def looks_like_headword(line: str) -> bool:
    return bool(KANJI_RE.search(line) or re.search(r"[\u3040-\u30ff]", line)) and not re.search(
        r"[A-Za-z]", line
    )


def is_kana_reading(line: str) -> bool:
    return bool(KANA_RE.fullmatch(line)) and bool(re.search(r"[\u3040-\u30ff]", line))


def extract_pdf_entries() -> list[dict]:
    files = {
        "N1": "VocabList.N1.pdf",
        "N2": "VocabList.N2.pdf",
        "N3": "VocabList.N3.pdf",
        "N4": "VocabList.N4.pdf",
        "N5": "VocabListN5.pdf",
    }
    result: list[dict] = []
    for level, filename in files.items():
        path = PDF_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing vocabulary PDF: {path}")
        document = fitz.open(path)
        for page_number, page in enumerate(document, start=1):
            lines = page_word_lines(page.get_text())
            index = 0
            while index < len(lines):
                if not looks_like_headword(lines[index]):
                    index += 1
                    continue
                word = lines[index]
                index += 1
                reading = word

                # Tanos wraps some slash-separated katakana spellings onto a second line.
                if "/" in word and index < len(lines) and is_kana_reading(lines[index]):
                    word += lines[index]
                    reading = word
                    index += 1
                elif index < len(lines) and is_kana_reading(lines[index]):
                    reading = lines[index]
                    index += 1

                meaning_lines = []
                while index < len(lines) and not looks_like_headword(lines[index]):
                    meaning_lines.append(lines[index])
                    index += 1
                meaning = re.sub(r"\s+", " ", " ".join(meaning_lines)).strip(" ,;")
                if word and meaning:
                    result.append(
                        {
                            "level": level,
                            "word": word,
                            "reading": reading,
                            "meaning": meaning,
                            "source": f"pdf:{filename}:p{page_number}",
                        }
                    )
        document.close()
    PDF_JSON.parent.mkdir(parents=True, exist_ok=True)
    PDF_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def open_key_index(entries: dict[str, dict]) -> dict[tuple[str, str], list[str]]:
    index: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entry_id, entry in entries.items():
        forms = [entry["word"], *entry.get("other_forms", [])]
        readings = [entry["reading"], *entry.get("other_readings", [])]
        for form in forms:
            for reading in readings:
                key = key_for(form, reading)
                if entry_id not in index[key]:
                    index[key].append(entry_id)
    return index


def merge_unique(items: list[str], value: str) -> None:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value:
        return
    marker = normalize(value).casefold()
    if all(normalize(item).casefold() != marker for item in items):
        items.append(value)


def meaning_tokens(value: str) -> set[str]:
    ignored = {"the", "and", "or", "to", "of", "in", "on", "a", "an", "be", "for", "with"}
    return {token for token in re.findall(r"[a-z0-9]+", str(value).casefold()) if token not in ignored}


def fallback_pos(word: str, reading: str, meaning: str, source_pos: list[str] | None = None) -> str:
    for pos in source_pos or []:
        if pos.startswith("v"):
            return "verb"
        if pos == "adj-i":
            return "adj-i"
        if pos.startswith("adj-na") or pos == "adj-no":
            return "adj-na"
        if pos.startswith("adv"):
            return "adverb"
        if pos == "int":
            return "interjection"
        if pos in {"prt", "suf", "pref", "aux", "ctr", "conj", "exp"}:
            return "expression"
    lower = meaning.casefold().strip()
    if re.search(r"[~～=＝]|[()（）]", word):
        return "expression"
    if lower.startswith("to "):
        return "verb"
    if lower.startswith(("oh!", "ah!", "hey!", "hello!")):
        return "interjection"
    if "adverb" in lower or lower.endswith("ly"):
        return "adverb"
    if "counter for" in lower or "suffix" in lower or "prefix" in lower or "particle" in lower:
        return "expression"
    if word.endswith("い") and reading.endswith("い"):
        return "adj-i"
    return "noun"


def expression_sentence(level: str, word: str, gloss: str) -> tuple[str, str]:
    """Give grammar patterns and affixes a concrete sentence showing their use."""
    w = re.sub(r"[\s()]", "", word)
    cases = [
        ("\u306b\u3064\u3044\u3066", "\u65e5\u672c\u306e\u6b74\u53f2\u306b\u3064\u3044\u3066\u8abf\u3079\u3066\u3044\u307e\u3059\u3002", "I am researching Japanese history."),
        ("\u3059\u304e", "\u6628\u591c\u306f\u30c6\u30ec\u30d3\u3092\u898b\u3059\u304e\u3066\u3001\u5bdd\u308b\u306e\u304c\u9045\u304f\u306a\u308a\u307e\u3057\u305f\u3002", "I watched too much television last night and went to bed late."),
        ("\u3053\u308d", "\u6bce\u65e5\u4e03\u6642\u3054\u308d\u306b\u5bb6\u306b\u5e30\u308a\u307e\u3059\u3002", "I get home at around seven every day."),
        ("\u306a\u304b", "\u6388\u696d\u4e2d\u306f\u643a\u5e2f\u96fb\u8a71\u3092\u4f7f\u308f\u306a\u3044\u3067\u304f\u3060\u3055\u3044\u3002", "Please do not use your phone during class."),
        ("\u304c\u3061", "\u5fd9\u3057\u3044\u3068\u3001\u671d\u98df\u3092\u629c\u304d\u304c\u3061\u3067\u3059\u3002", "When I am busy, I tend to skip breakfast."),
        ("\u304d\u308b", "\u5f7c\u5973\u306f\u6700\u5f8c\u307e\u3067\u5168\u529b\u3067\u8d70\u308a\u304d\u3063\u305f\u3002", "She ran with all her strength to the very end."),
        ("\u3068\u3053\u308d", "\u4eca\u304b\u3089\u53cb\u9054\u306e\u5bb6\u3078\u51fa\u304b\u3051\u308b\u3068\u3053\u308d\u3067\u3059\u3002", "I am just about to leave for my friend's house."),
        ("\u306a\u3093\u304b", "\u3053\u3093\u306a\u7c21\u5358\u306a\u554f\u984c\u3001\u5b50\u3069\u3082\u306a\u3093\u304b\u3067\u3082\u89e3\u3051\u307e\u3059\u3002", "Even a child can solve a problem this simple."),
        ("\u3057\u304d", "\u3053\u306e\u5bb6\u306f\u548c\u98a8\u306e\u69d8\u5f0f\u3067\u5efa\u3066\u3089\u308c\u3066\u3044\u307e\u3059\u3002", "This house was built in a Japanese style."),
        ("\u3055\u307e", "\u3053\u306e\u66f8\u985e\u306f\u7530\u4e2d\u69d8\u5b9b\u306b\u304a\u9001\u308a\u3057\u307e\u3059\u3002", "We will send these documents to Mr. Tanaka."),
        ("\u307e\u308b\u3054\u3068", "\u53ce\u7a6b\u3057\u305f\u679c\u7269\u3092\u7bb1\u3054\u3068\u8cb7\u3044\u307e\u3057\u305f\u3002", "I bought the whole box of harvested fruit."),
        ("\u306b\u3088\u308b\u3068", "\u5929\u6c17\u4e88\u5831\u306b\u3088\u308b\u3068\u3001\u660e\u65e5\u306f\u96e8\u3060\u305d\u3046\u3067\u3059\u3002", "According to the forecast, it will rain tomorrow."),
        ("\u3066\u3057\u307e\u3046", "\u96fb\u8eca\u306e\u4e2d\u306b\u5098\u3092\u5fd8\u308c\u3066\u3057\u307e\u3044\u307e\u3057\u305f\u3002", "I accidentally left my umbrella on the train."),
        ("\u3066\u304a\u304d", "\u65c5\u884c\u306e\u524d\u306b\u3001\u5207\u7b26\u3092\u8cb7\u3063\u3066\u304a\u304d\u307e\u3059\u3002", "I will buy the tickets before the trip."),
        ("\u3066\u304a\u304f", "\u4f1a\u8b70の前に、資料を印刷しておきます。", "I will print the materials before the meeting."),
        ("\u304aわる", "\u30ecポートを書き終わってから、友達と食事しました。", "After finishing the report, I had a meal with a friend."),
        ("\u59cbめる", "\u6765月から新しい仕事をし始めます。", "I will start a new job next month."),
        ("\u304a\u304d", "\u3053\u306e\u85ac\u306f\u3001\u98df\u4e8b\u306e\u3042\u3068\u4e09\u6642\u9593\u304a\u304d\u306b\u98f2\u3093\u3067\u304f\u3060\u3055\u3044\u3002", "Take this medicine every three hours after a meal."),
        ("\u3060\u3059", "\u591c\u4e2d\u306b\u96e8\u304c\u964d\u308a\u3060\u3057\u307e\u3057\u305f\u3002", "It started raining in the middle of the night."),
        ("\u306b\u304f\u3044", "\u5b57\u304c\u5c0f\u3055\u304f\u3066\u3001\u3053\u306e\u5730\u56f3\u306f\u8aad\u307f\u306b\u304f\u3044\u3067\u3059\u3002", "The print is small, so this map is hard to read."),
        ("\u3084\u3059\u3044", "\u7d75\u304c\u591a\u3044\u306e\u3067\u3001\u3053\u306e\u672c\u306f\u8aad\u307f\u3084\u3059\u3044\u3067\u3059\u3002", "This book is easy to read because it has many pictures."),
        ("\u3070\u304b\u308a", "\u5f1f\u306f\u3055\u3063\u304d\u5bb6\u306b\u5e30\u3063\u3066\u304d\u305f\u3070\u304b\u308a\u3067\u3059\u3002", "My younger brother just got home."),
        ("\u307e\u307e", "\u7a93\u3092\u958b\u3051\u305f\u307e\u307e\u5bb6\u3092\u51fa\u3066\u3057\u307e\u3044\u307e\u3057\u305f\u3002", "I left home with the window still open."),
        ("\u3069\u3053\u308d\u304b", "\u5f7c\u306f\u8b1d\u308b\u3069\u3053\u308d\u304b\u3001\u307e\u3059\u307e\u3059\u6587\u53e5\u3092\u8a00\u3063\u305f\u3002", "Far from apologizing, he complained even more."),
        ("\u307e\u307f\u308c", "\u5b50\u3069\u3082\u305f\u3061\u306f\u6ce5\u307e\u307f\u308c\u306b\u306a\u3063\u3066\u5e30\u3063\u3066\u304d\u305f\u3002", "The children came home covered in mud."),
        ("\u304d\u308a", "\u5352\u696d\u3057\u3066\u304b\u3089\u3001\u5f7c\u306b\u306f\u4e00\u5ea6\u4f1a\u3063\u305f\u304d\u308a\u3060\u3002", "I have only met him once since graduation."),
        ("\u3061\u3087\u3046", "\u99c5\u306e\u524d\u3067\u53cb\u9054\u3068\u5f85\u3061\u5408\u308f\u305b\u3092\u3057\u307e\u3057\u305f\u3002", "I arranged to meet my friend in front of the station."),
    ]
    for marker, ja, en in cases:
        if marker in w:
            return ja, en
    for marker, ja, en in [
        ("\u304a\u3057\u307e\u3044", "\u7d50\u5c40\u3001\u5f7c\u306f\u8aac\u660e\u3092\u7d9a\u3051\u3089\u308c\u305a\u3058\u307e\u3044\u3060\u3063\u305f\u3002", "In the end, he could not continue explaining."),
        ("\u307b\u3046", "\u4eca\u5e74\u306e\u58f2\u4e0a\u306f\u3001\u524d\u5e74\u3068\u6bd4\u3079\u308b\u3068\u5897\u3048\u305f\u307b\u3046\u3060\u3002", "Sales were higher this year compared with last year."),
        ("\u3082\u3061", "\u5f7c\u306f\u8a00\u8a9e\u306e\u624d\u80fd\u306e\u6301\u3061\u4e3b\u3060\u3002", "He is a person with a talent for languages."),
        ("\u3057\u305f", "\u8abf\u67fb\u306f\u5c02\u9580\u5bb6\u306e\u7ba1\u7406\u4e0b\u3067\u884c\u308f\u308c\u305f\u3002", "The survey was conducted under expert supervision."),
        ("\u4ed8", "\u4e00\u5e74\u4fdd\u8a3c\u4ed8\u304d\u306e\u30d1\u30bd\u30b3\u30f3\u3092\u9078\u3073\u307e\u3057\u305f\u3002", "I chose a computer with a one-year warranty."),
        ("\u6c17\u5473", "\u98a8\u90aa\u6c17\u5473\u306a\u306e\u3067\u3001\u4eca\u65e5\u306f\u65e9\u304f\u5e30\u308a\u307e\u3059\u3002", "I feel like I am coming down with a cold, so I will go home early."),
        ("\u7d9a\u304f", "\u96e8\u306e\u65e5\u304c\u4e09\u65e5\u7d9a\u3044\u3066\u3001\u5ddd\u306e\u6c34\u4f4d\u304c\u4e0a\u304c\u3063\u305f\u3002", "After three days of rain, the river level rose."),
        ("\u901a\u308a", "\u53d6\u6271\u8aac\u660e\u66f8\u306e\u901a\u308a\u306b\u64cd\u4f5c\u3057\u3066\u304f\u3060\u3055\u3044\u3002", "Please operate it according to the manual."),
        ("\u904e\u304e\u308b", "\u7518\u3044\u7269\u3092\u98df\u3079\u904e\u304e\u3066\u3001\u304a\u8179\u304c\u75db\u304f\u306a\u3063\u305f\u3002", "I ate too many sweets and got a stomachache."),
        ("\u96e3\u3044", "\u5f7c\u304c\u304b\u3051\u3066\u304f\u308c\u305f\u89aa\u5207\u306f\u5fd8\u308c\u304c\u305f\u3044\u3002", "It is hard to forget his kindness."),
        ("\u8f9b\u3044", "\u3053\u306e\u30b9\u30fc\u30d7\u306f\u5869\u8f9b\u304f\u3066\u98f2\u307f\u306b\u304f\u3044\u3002", "This soup is too salty to drink easily."),
        ("\u9063\u3044", "\u5f7c\u5973\u306e\u8a00\u8449\u9063\u3044\u306f\u3068\u3066\u3082\u4e01\u5be7\u3060\u3002", "Her choice of words is very polite."),
    ]:
        if marker in w:
            return ja, en
    prefix_examples = [
        ("\u4e00", "\u4eca\u5e74\u306f\u58f2\u4e0a\u304c\u4e00\u6bb5\u3068\u4f38\u3073\u305f\u3002", "Sales increased even more this year."),
        ("\u4e0a", "\u4e0a\u53f8\u306b\u8abf\u67fb\u306e\u7d50\u679c\u3092\u5831\u544a\u3057\u305f\u3002", "I reported the survey results to my supervisor."),
        ("\u4e21", "\u4e21\u89aa\u306f\u6765\u6708\u65e5\u672c\u306b\u6765\u308b\u4e88\u5b9a\u3067\u3059\u3002", "Both of my parents plan to visit Japan next month."),
        ("\u4eca", "\u4eca\u5e74\u306f\u65b0\u3057\u3044\u7814\u7a76\u306b\u53d6\u308a\u7d44\u3093\u3067\u3044\u308b\u3002", "This year I am working on a new research project."),
        ("\u4f4e", "\u4f4e\u6c17\u5727\u306e\u5f71\u97ff\u3067\u3001\u660e\u65e5\u306f\u5929\u6c17\u304c\u5d29\u308c\u308b\u3089\u3057\u3044\u3002", "A low-pressure system is expected to bring bad weather tomorrow."),
        ("\u518d", "\u58ca\u308c\u305f\u8a2d\u5099\u3092\u518d\u5229\u7528\u3059\u308b\u65b9\u6cd5\u3092\u8003\u3048\u3066\u3044\u308b\u3002", "We are considering ways to reuse the broken equipment."),
        ("\u521d", "\u521d\u5bfe\u9762\u306e\u5370\u8c61\u306f\u3068\u3066\u3082\u3088\u304b\u3063\u305f\u3002", "My first impression when we met was very positive."),
        ("\u524d", "\u524d\u5e74\u5ea6\u306e\u8cc7\u6599\u3092\u53c2\u8003\u306b\u4eca\u5e74\u306e\u6848\u3092\u4f5c\u3063\u305f\u3002", "I used last year's materials when preparing this year's proposal."),
        ("\u526f", "\u526f\u793e\u9577\u304c\u4f1a\u8b70\u306e\u958b\u4f1a\u3092\u5ba3\u8a00\u3057\u305f\u3002", "The vice president opened the meeting."),
        ("\u53cd", "\u8a08\u753b\u306b\u53cd\u5bfe\u3059\u308b\u4f4f\u6c11\u306e\u610f\u898b\u3082\u805e\u3044\u305f\u3002", "We also heard the opinions of residents who opposed the plan."),
        ("\u5404", "\u5404\u56fd\u306e\u4ee3\u8868\u304c\u74b0\u5883\u4f1a\u8b70\u306b\u53c2\u52a0\u3057\u305f\u3002", "Representatives from each country attended the environmental conference."),
        ("\u540c", "\u4e21\u793e\u306f\u540c\u610f\u306e\u4e0a\u3067\u65b0\u3057\u3044\u5951\u7d04\u3092\u7d50\u3093\u3060\u3002", "The two companies signed a new contract by mutual agreement."),
        ("\u540d", "\u540d\u4f5c\u3068\u547c\u3070\u308c\u308b\u6620\u753b\u3092\u5927\u753b\u9762\u3067\u898b\u305f\u3002", "I watched a film considered a masterpiece on the big screen."),
        ("\u548c", "\u4f1a\u98df\u3067\u306f\u548c\u98df\u3092\u7528\u610f\u3057\u307e\u3059\u3002", "We will serve Japanese food at the reception."),
        ("\u5916", "\u5916\u56fd\u306e\u4e8b\u4f8b\u3082\u8abf\u3079\u3066\u3001\u6bd4\u8f03\u8868\u3092\u4f5c\u3063\u305f\u3002", "I also researched foreign examples and made a comparison table."),
        ("\u5c0f", "\u5c0f\u5b66\u751f\u5411\u3051\u306e\u8aad\u307f\u7269\u3092\u4f5c\u308a\u307e\u3057\u305f\u3002", "I made a reading book for elementary school students."),
        ("\u6700", "\u6700\u512a\u5148\u306e\u8ab2\u984c\u304b\u3089\u89e3\u6c7a\u3057\u3066\u3044\u3053\u3046\u3002", "Let's resolve the highest-priority issue first."),
        ("\u6bce", "\u6bce\u9031\u3001\u9032\u6357\u3092\u78ba\u8a8d\u3059\u308b\u4f1a\u8b70\u3092\u884c\u3063\u3066\u3044\u308b\u3002", "We hold a weekly meeting to check progress."),
        ("\u73fe", "\u73fe\u793e\u9577\u306f\u5275\u696d\u6642\u304b\u3089\u4f1a\u793e\u306b\u52e4\u3081\u3066\u3044\u308b\u3002", "The current president has worked at the company since it was founded."),
        ("\u77ed", "\u77ed\u671f\u9593\u3067\u7d50\u679c\u3092\u51fa\u3059\u305f\u3081\u3001\u4f5c\u696d\u3092\u898b\u76f4\u3057\u305f\u3002", "We reviewed our work to get results in a short period."),
        ("\u7dcf", "\u7dcf\u984d\u3092\u78ba\u8a8d\u3057\u3066\u304b\u3089\u3001\u4e88\u7b97\u3092\u7533\u8acb\u3057\u3066\u304f\u3060\u3055\u3044\u3002", "Check the total amount before submitting the budget request."),
        ("\u7fcc", "\u7fcc\u65e5\u3001\u62c5\u5f53\u8005\u304b\u3089\u8a73\u3057\u3044\u8aac\u660e\u304c\u3042\u3063\u305f\u3002", "The person in charge gave a detailed explanation the next day."),
        ("\u8af8", "\u8af8\u554f\u984c\u3092\u6574\u7406\u3057\u305f\u4e0a\u3067\u3001\u89e3\u6c7a\u7b56\u3092\u63d0\u6848\u3057\u305f\u3002", "After organizing the various issues, we proposed a solution."),
        ("\u91cd", "\u91cd\u8981\u306a\u8cc7\u6599\u306f\u5225\u306e\u5834\u6240\u306b\u4fdd\u5b58\u3057\u3066\u304f\u3060\u3055\u3044\u3002", "Please save important documents somewhere else."),
        ("\u9632", "\u5730\u57df\u3067\u9632\u707d\u8a13\u7df4\u304c\u884c\u308f\u308c\u305f\u3002", "A disaster-prevention drill was held in the community."),
        ("\u9ad8", "\u9ad8\u53ce\u5165\u306e\u5bb6\u5ead\u306b\u3082\u7269\u4fa1\u4e0a\u6607\u306e\u5f71\u97ff\u304c\u53ca\u3093\u3067\u3044\u308b\u3002", "Rising prices are affecting high-income households too."),
    ]
    for marker, ja, en in prefix_examples:
        prefix = w.lstrip("~\uFF5E\u301C")
        if prefix and prefix.startswith(marker):
            return ja, en
    if w.endswith("\u5bb6") and "specialized" in gloss.casefold():
        return "\u5f7c\u306f\u74b0\u5883\u554f\u984c\u306e\u5c02\u9580\u5bb6\u3068\u3057\u3066\u77e5\u3089\u308c\u3066\u3044\u307e\u3059\u3002", "He is known as a specialist in environmental issues."
    if "\u3054\u3056\u3044\u307e\u3059" in w:
        return "\u3053\u3061\u3089\u304c\u4f1a\u8b70\u5ba4\u3067\u3054\u3056\u3044\u307e\u3059\u3002", "The meeting room is this way."
    if "\u3061\u3083\u3093" in w:
        return "\u82b1\u3061\u3083\u3093\u306f\u4eca\u65e5\u3082\u5143\u6c17\u3067\u3059\u3002", "Hana is full of energy today, too."
    if "\u3054\u3068" in w:
        return "\u3053\u306e\u30af\u30e9\u30b9\u306f\u4e09\u65e5\u3054\u3068\u306b\u30c6\u30b9\u30c8\u304c\u3042\u308a\u307e\u3059\u3002", "This class has a test every three days."
    if "\u307e\u307e" in w:
        return "\u7a93\u3092\u958b\u3051\u305f\u307e\u307e\u5bb6\u3092\u51fa\u3066\u3057\u307e\u3044\u307e\u3057\u305f\u3002", "I left home with the window still open."
    if "\u306f\u305a" in w:
        return "\u96fb\u8eca\u306f\u3082\u3046\u99c5\u306b\u7740\u3044\u305f\u306f\u305a\u3067\u3059\u3002", "The train should have arrived at the station by now."
    endings = [
        ("\u304c\u308b", "\u5b50\u3069\u3082\u306f\u65b0\u3057\u3044\u304a\u3082\u3061\u3083\u3092\u6b32\u3057\u304c\u3063\u3066\u3044\u307e\u3059\u3002", "The child wants a new toy."),
        ("\u305a\u3064", "\u30af\u30c3\u30ad\u30fc\u3092\u4e00\u4eba\u306b\u4e8c\u3064\u305a\u3064\u914d\u308a\u307e\u3057\u305f\u3002", "I gave each person two cookies."),
        ("\u305f\u3061", "\u5b50\u3069\u3082\u305f\u3061\u306f\u516c\u5712\u3067\u904a\u3093\u3067\u3044\u307e\u3059\u3002", "The children are playing in the park."),
        ("\u3060\u3051", "\u4eca\u65e5\u306f\u6c34\u3060\u3051\u98f2\u307f\u307e\u3057\u305f\u3002", "I only drank water today."),
        ("\u306a\u3069", "\u30ce\u30fc\u30c8\u3084\u30da\u30f3\u306a\u3069\u3092\u8cb7\u3044\u307e\u3057\u305f\u3002", "I bought notebooks, pens, and other things."),
        ("\u5074", "\u99c5\u306e\u6771\u5074\u306b\u65b0\u3057\u3044\u5e97\u304c\u3067\u304d\u307e\u3057\u305f\u3002", "A new shop opened on the east side of the station."),
        ("\u5186", "\u3053\u306e\u30d1\u30f3\u306f\u767e\u5186\u3067\u3059\u3002", "This bread costs one hundred yen."),
        ("\u518a", "\u56f3\u66f8\u9928\u3067\u672c\u3092\u4e8c\u518a\u501f\u308a\u307e\u3057\u305f\u3002", "I borrowed two books from the library."),
        ("\u5206", "\u99c5\u307e\u3067\u6b69\u3044\u3066\u5341\u5206\u304b\u304b\u308a\u307e\u3059\u3002", "It takes ten minutes to walk to the station."),
        ("\u524d", "\u5b66\u6821\u306e\u524d\u3067\u53cb\u9054\u3092\u5f85\u3061\u307e\u3057\u305f\u3002", "I waited for my friend in front of the school."),
        ("\u5339", "\u5bb6\u3067\u732b\u3092\u4e8c\u5339\u98fc\u3063\u3066\u3044\u307e\u3059\u3002", "I have two cats at home."),
        ("\u53f0", "\u99d0\u8eca\u5834\u306b\u8eca\u304c\u4e8c\u53f0\u6b62\u307e\u3063\u3066\u3044\u307e\u3059\u3002", "Two cars are parked in the lot."),
        ("\u56de", "\u3053\u306e\u6620\u753b\u3092\u4e09\u56de\u898b\u307e\u3057\u305f\u3002", "I have seen this movie three times."),
        ("\u5c4b", "\u99c5\u306e\u8fd1\u304f\u306e\u82b1\u5c4b\u3067\u82b1\u3092\u8cb7\u3044\u307e\u3057\u305f\u3002", "I bought flowers at a shop near the station."),
        ("\u5e74", "\u65e5\u672c\u306b\u4e8c\u5e74\u4f4f\u3093\u3067\u3044\u307e\u3059\u3002", "I have lived in Japan for two years."),
        ("\u65e5", "\u4f11\u307f\u3092\u4e09\u65e5\u9593\u53d6\u308a\u307e\u3057\u305f\u3002", "I took three days off."),
        ("\u6642", "\u6bce\u671d\u4e03\u6642\u306b\u5bb6\u3092\u51fa\u307e\u3059\u3002", "I leave home at seven every morning."),
        ("\u6642\u9593", "\u6bce\u65e5\u4e8c\u6642\u9593\u65e5\u672c\u8a9e\u3092\u52c9\u5f37\u3057\u307e\u3059\u3002", "I study Japanese for two hours every day."),
        ("\u672c", "\u6c34\u3092\u4e8c\u672c\u8cb7\u3063\u3066\u304d\u307e\u3057\u305f\u3002", "I bought two bottles of water."),
        ("\u676f", "\u98df\u5f8c\u306b\u304a\u8336\u3092\u4e00\u676f\u98f2\u307f\u307e\u3057\u305f\u3002", "I had a cup of tea after the meal."),
        ("\u679a", "\u7533\u8fbc\u66f8\u3092\u4e09\u679a\u66f8\u3044\u3066\u304f\u3060\u3055\u3044\u3002", "Please fill out three application forms."),
        ("\u756a", "\u4eca\u5e74\u306e\u4e00\u756a\u597d\u304d\u306a\u6620\u753b\u306f\u4f55\u3067\u3059\u304b\u3002", "What is your favorite movie this year?"),
        ("\u8a9e", "\u65c5\u884c\u3067\u5f79\u306b\u7acb\u3064\u65e5\u672c\u8a9e\u3092\u7fd2\u3063\u3066\u3044\u307e\u3059\u3002", "I am learning Japanese that is useful for traveling."),
        ("\u9031\u9593", "\u3053\u306e\u30b3\u30fc\u30b9\u306f\u4e8c\u9031\u9593\u7d9a\u304d\u307e\u3059\u3002", "This course lasts two weeks."),
        ("\u968e", "\u6559\u5ba4\u306f\u5efa\u7269\u306e\u4e09\u968e\u306b\u3042\u308a\u307e\u3059\u3002", "The classroom is on the third floor."),
        ("\u4f1a", "\u571f\u66dc\u65e5\u306b\u8aad\u66f8\u4f1a\u306b\u53c2\u52a0\u3057\u307e\u3059\u3002", "I will attend a reading group on Saturday."),
        ("\u533a", "\u6765\u5e74\u3001\u65b0\u5bbf\u533a\u306b\u5f15\u3063\u8d8a\u3059\u4e88\u5b9a\u3067\u3059\u3002", "I plan to move to Shinjuku Ward next year."),
        ("\u541b", "\u9234\u6728\u541b\u306f\u540c\u3058\u30af\u30e9\u30b9\u306e\u53cb\u9054\u3067\u3059\u3002", "Suzuki is a friend in my class."),
        ("\u54e1", "\u5144\u306f\u99c5\u54e1\u3068\u3057\u3066\u50cd\u3044\u3066\u3044\u307e\u3059\u3002", "My older brother works as a station attendant."),
        ("\u5b66\u90e8", "\u59c9\u306f\u6587\u5b66\u90e8\u3067\u65e5\u672c\u6587\u5b66\u3092\u5b66\u3093\u3067\u3044\u307e\u3059\u3002", "My older sister studies Japanese literature in the literature department."),
        ("\u88fd", "\u3053\u306e\u30ab\u30e1\u30e9\u306f\u65e5\u672c\u88fd\u3067\u3059\u3002", "This camera is made in Japan."),
        ("\u5411\u3051", "\u3053\u306e\u6559\u6750\u306f\u521d\u5fc3\u8005\u5411\u3051\u3067\u3059\u3002", "This material is for beginners."),
        ("\u5207\u308c", "\u4eba\u6c17\u306e\u30c1\u30b1\u30c3\u30c8\u306f\u3059\u3067\u306b\u58f2\u308a\u5207\u308c\u307e\u3057\u305f\u3002", "The popular tickets have already sold out."),
        ("\u5185", "\u53d7\u4ed8\u6642\u9593\u5185\u306b\u7533\u8fbc\u3092\u6e08\u307e\u305b\u3066\u304f\u3060\u3055\u3044\u3002", "Please complete your application within office hours."),
        ("\u5916", "\u305d\u306e\u51fa\u8cbb\u306f\u4eca\u56de\u306e\u8abf\u67fb\u306e\u5bfe\u8c61\u5916\u3067\u3059\u3002", "That expense is outside the scope of this survey."),
        ("\u3060\u3089\u3051", "\u96e8\u3067\u9774\u304c\u6ce5\u3060\u3089\u3051\u306b\u306a\u3063\u305f\u3002", "My shoes got covered in mud from the rain."),
        ("\u307d\u3044", "\u305d\u306e\u8a00\u3044\u65b9\u306f\u5b50\u3069\u3082\u3063\u307d\u3044\u3067\u3059\u3002", "That way of speaking sounds childish."),
        ("\u307f\u305f\u3044", "\u5916\u306f\u96e8\u304c\u964d\u3063\u3066\u3044\u308b\u307f\u305f\u3044\u3067\u3059\u3002", "It looks like it is raining outside."),
        ("\u5316", "\u3053\u306e\u5730\u57df\u3067\u306f\u9ad8\u9f62\u5316\u304c\u9032\u3093\u3067\u3044\u307e\u3059\u3002", "The population is aging in this region."),
        ("\u611f", "\u3053\u306e\u5e97\u306e\u5bb6\u5177\u306f\u9ad8\u7d1a\u611f\u304c\u3042\u308a\u307e\u3059\u3002", "The furniture in this shop has a luxurious feel."),
        ("\u6599", "\u7f8e\u8853\u9928\u306e\u5165\u5834\u6599\u306f\u5343\u5186\u3067\u3059\u3002", "Admission to the art museum costs one thousand yen."),
        ("\u5b9b", "\u3053\u306e\u5c01\u7b52\u306f\u7530\u4e2d\u3055\u3093\u5b9b\u3067\u3059\u3002", "This envelope is addressed to Mr. Tanaka."),
        ("\u7db2", "\u305d\u306e\u4f1a\u793e\u306f\u5168\u56fd\u306b\u8ca9\u58f2\u7db2\u3092\u5e83\u3052\u305f\u3002", "The company expanded its sales network nationwide."),
        ("\u8a3c", "\u5165\u9928\u6642\u306b\u8eab\u5206\u8a3c\u3092\u898b\u305b\u3066\u304f\u3060\u3055\u3044\u3002", "Please show your identification when entering."),
    ]
    endings.extend([
        ("\u500b", "\u679c\u7269\u3092\u4e09\u500b\u8cb7\u3063\u3066\u3001\u53cb\u9054\u3068\u5206\u3051\u307e\u3057\u305f\u3002", "I bought three pieces of fruit and shared them with a friend."),
        ("\u6b73", "\u59b9\u306f\u6765\u6708\u3067\u5341\u516b\u6b73\u306b\u306a\u308a\u307e\u3059\u3002", "My younger sister will turn eighteen next month."),
        ("\u6708", "\u65e5\u672c\u3067\u306f\u56db\u6708\u306b\u65b0\u5b66\u671f\u304c\u59cb\u307e\u308a\u307e\u3059\u3002", "The new school year starts in April in Japan."),
        ("\u4e01\u76ee", "\u99c5\u3092\u51fa\u3066\u3001\u4e8c\u4e01\u76ee\u306e\u89d2\u3092\u5de6\u306b\u66f2\u304c\u3063\u3066\u304f\u3060\u3055\u3044\u3002", "Leave the station and turn left at the second block."),
        ("\u4f4d", "\u5f7c\u306f\u5168\u56fd\u5927\u4f1a\u3067\u4e09\u4f4d\u306b\u5165\u308a\u307e\u3057\u305f\u3002", "He placed third in the national competition."),
        ("\u5146", "\u4eca\u5e74\u306e\u56fd\u306e\u4e88\u7b97\u306f\u4e00\u767e\u5146\u5186\u3092\u8d85\u3048\u307e\u3059\u3002", "This year's national budget exceeds one hundred trillion yen."),
        ("\u520a", "\u9031\u520a\u8a8c\u306e\u6700\u65b0\u53f7\u3092\u99c5\u306e\u58f2\u5e97\u3067\u8cb7\u3044\u307e\u3057\u305f\u3002", "I bought the latest issue of the weekly magazine at the station."),
        ("\u529b", "\u8a9e\u5b66\u529b\u3092\u4f38\u3070\u3059\u305f\u3081\u3001\u6bce\u65e5\u65b0\u3057\u3044\u6587\u7ae0\u3092\u8aad\u3093\u3067\u3044\u307e\u3059\u3002", "I read a new passage every day to improve my language ability."),
        ("\u52dd", "\u4eca\u5b63\u306f\u4e09\u52dd\u4e8c\u6557\u3067\u7d42\u3048\u307e\u3057\u305f\u3002", "We finished the season with three wins and two losses."),
        ("\u53e3", "\u6771\u53e3\u3067\u53cb\u4eba\u3068\u5f85\u3061\u5408\u308f\u305b\u307e\u3057\u305f\u3002", "I met my friend at the east exit."),
        ("\u53f2", "\u5927\u5b66\u3067\u65e5\u672c\u53f2\u3092\u5b66\u3093\u3067\u3044\u307e\u3059\u3002", "I study Japanese history at university."),
        ("\u540d", "\u4f1a\u8b70\u306b\u306f\u5404\u90e8\u9580\u304b\u3089\u4e8c\u540d\u305a\u3064\u53c2\u52a0\u3057\u307e\u3057\u305f\u3002", "Two people from each department attended the meeting."),
        ("\u554f", "\u8a66\u9a13\u306e\u7b2c\u4e00\u554f\u306f\u57fa\u672c\u7684\u306a\u5185\u5bb9\u3067\u3057\u305f\u3002", "The first question on the exam covered basic material."),
        ("\u5668", "\u6545\u969c\u3057\u305f\u6e2c\u5b9a\u5668\u3092\u4fee\u7406\u3057\u3066\u304b\u3089\u3001\u5b9f\u9a13\u3092\u518d\u958b\u3057\u305f\u3002", "After repairing the broken measuring device, we resumed the experiment."),
        ("\u56e3", "\u5730\u5143\u306e\u5287\u56e3\u304c\u6765\u6708\u65b0\u3057\u3044\u516c\u6f14\u3092\u884c\u3046\u3002", "The local theater group will perform a new play next month."),
        ("\u56fd", "\u4e21\u56fd\u306f\u74b0\u5883\u554f\u984c\u3067\u5354\u529b\u3059\u308b\u3053\u3068\u306b\u5408\u610f\u3057\u305f\u3002", "The two countries agreed to cooperate on environmental issues."),
        ("\u570f", "\u9996\u90fd\u570f\u3067\u306f\u901a\u52e4\u6642\u9593\u304c\u9577\u304f\u306a\u308a\u304c\u3061\u3060\u3002", "Commutes tend to be long in the metropolitan area."),
        ("\u5712", "\u690d\u7269\u5712\u3067\u5b63\u7bc0\u306e\u82b1\u3092\u697d\u3057\u3093\u3060\u3002", "We enjoyed the seasonal flowers at the botanical garden."),
        ("\u5834", "\u4f1a\u5834\u306e\u5165\u53e3\u3067\u53c2\u52a0\u8a3c\u3092\u63d0\u793a\u3057\u3066\u304f\u3060\u3055\u3044\u3002", "Please show your pass at the venue entrance."),
        ("\u5ba4", "\u7814\u7a76\u5ba4\u3067\u8abf\u67fb\u306e\u30c7\u30fc\u30bf\u3092\u6574\u7406\u3057\u307e\u3057\u305f\u3002", "I organized the research data in the laboratory."),
        ("\u5bfa", "\u4eac\u90fd\u306e\u53e4\u5bfa\u3092\u8a2a\u308c\u3001\u9577\u3044\u6b74\u53f2\u306b\u3064\u3044\u3066\u5b66\u3093\u3060\u3002", "I visited an old temple in Kyoto and learned about its long history."),
        ("\u5c71", "\u5bcc\u58eb\u5c71\u306e\u5c71\u9802\u304b\u3089\u65e5\u306e\u51fa\u3092\u898b\u307e\u3057\u305f\u3002", "I watched the sunrise from the summit of Mount Fuji."),
        ("\u5cf6", "\u4f11\u307f\u306e\u9593\u306b\u3001\u702c\u6238\u5185\u6d77\u306e\u5c0f\u3055\u306a\u5cf6\u3092\u8239\u3067\u5de1\u308a\u307e\u3057\u305f\u3002", "During the break, we visited the islands of the Seto Inland Sea by boat."),
        ("\u5dfb", "\u9577\u7de8\u5c0f\u8aac\u306e\u4e0b\u5dfb\u3092\u4e00\u6c17\u306b\u8aad\u3093\u3060\u3002", "I read the second volume of the long novel in one sitting."),
        ("\u5e2d", "\u6b8b\u308a\u306e\u5e2d\u306f\u4e09\u5e2d\u3060\u3051\u3060\u3063\u305f\u306e\u3067\u3001\u65e9\u3081\u306b\u4e88\u7d04\u3057\u305f\u3002", "Only three seats were left, so I booked early."),
        ("\u5e33", "\u624b\u5e33\u306b\u4f1a\u8b70\u306e\u65e5\u7a0b\u3092\u66f8\u304d\u8fbc\u307f\u307e\u3057\u305f\u3002", "I wrote the meeting schedule in my planner."),
        ("\u5e81", "\u6c17\u8c61\u5e81\u306f\u5927\u96e8\u3078\u306e\u8b66\u6212\u3092\u547c\u3073\u304b\u3051\u305f\u3002", "The meteorological agency issued a warning about heavy rain."),
        ("\u5f01", "\u5730\u5143\u306e\u65b9\u8a00\u3067\u8a71\u3057\u304b\u3051\u3089\u308c\u3066\u3001\u6700\u521d\u306f\u610f\u5473\u304c\u5206\u304b\u3089\u306a\u304b\u3063\u305f\u3002", "Someone spoke to me in the local dialect, and at first I could not understand."),
        ("\u5f62", "\u3053\u306e\u56f3\u5f62\u306f\u5de6\u53f3\u5bfe\u79f0\u306b\u306a\u3063\u3066\u3044\u307e\u3059\u3002", "This figure is symmetrical."),
        ("\u6226", "\u6c7a\u52dd\u6226\u3067\u306f\u6700\u5f8c\u307e\u3067\u63a5\u6226\u304c\u7d9a\u3044\u305f\u3002", "The final match remained close until the end."),
        ("\u6240", "\u96c6\u5408\u5834\u6240\u306f\u99c5\u306e\u6539\u672d\u53e3\u3067\u3059\u3002", "The meeting point is at the station ticket gate."),
        ("\u624b", "\u5f7c\u306f\u5b88\u5099\u306e\u4e0a\u624b\u306a\u9078\u624b\u3068\u3057\u3066\u77e5\u3089\u308c\u3066\u3044\u308b\u3002", "He is known as a player with excellent defense."),
        ("\u6559", "\u5730\u57df\u4f4f\u6c11\u306f\u5b97\u6559\u306e\u9055\u3044\u3092\u8d8a\u3048\u3066\u5354\u529b\u3057\u305f\u3002", "People in the community cooperated across religious differences."),
        ("\u671f", "\u9577\u671f\u7684\u306a\u8996\u70b9\u304b\u3089\u8a08\u753b\u3092\u898b\u76f4\u3059\u5fc5\u8981\u304c\u3042\u308b\u3002", "We need to review the plan from a long-term perspective."),
        ("\u6761", "\u5951\u7d04\u66f8\u306e\u7b2c\u4e09\u6761\u306b\u89e3\u7d04\u306e\u6761\u4ef6\u304c\u8a18\u3055\u308c\u3066\u3044\u308b\u3002", "The cancellation terms are written in Article 3 of the contract."),
        ("\u6821", "\u5e02\u5185\u306e\u4e09\u6821\u304c\u5408\u540c\u3067\u97f3\u697d\u4f1a\u3092\u958b\u3044\u305f\u3002", "Three schools in the city held a joint music event."),
        ("\u696d", "\u88fd\u9020\u696d\u3067\u306f\u4eba\u624b\u4e0d\u8db3\u304c\u5927\u304d\u306a\u8ab2\u984c\u306b\u306a\u3063\u3066\u3044\u308b\u3002", "A shortage of workers has become a major issue in manufacturing."),
        ("\u6a5f", "\u65c5\u5ba2\u6a5f\u306f\u4e88\u5b9a\u3088\u308a\u4e09\u5341\u5206\u9045\u308c\u3066\u51fa\u767a\u3057\u305f\u3002", "The passenger plane departed thirty minutes late."),
        ("\u6b4c", "\u307f\u3093\u306a\u3067\u5fdc\u63f4\u6b4c\u3092\u6b4c\u3063\u3066\u3001\u9078\u624b\u3092\u52b1\u307e\u3057\u305f\u3002", "We sang a team song to encourage the players."),
        ("\u6b69", "\u5fa9\u8208\u306b\u5411\u3051\u305f\u7b2c\u4e00\u6b69\u3092\u8e0f\u307f\u51fa\u3057\u305f\u3002", "We took the first step toward recovery."),
        ("\u6cbf\u3044", "\u5ddd\u6cbf\u3044\u306b\u65b0\u3057\u3044\u904a\u6b69\u9053\u304c\u6574\u5099\u3055\u308c\u305f\u3002", "A new walking path was built along the river."),
        ("\u6cca", "\u4eac\u90fd\u3067\u4e8c\u6cca\u3057\u3066\u304b\u3089\u3001\u5948\u826f\u3078\u5411\u304b\u3063\u305f\u3002", "After staying two nights in Kyoto, we headed to Nara."),
        ("\u6d41", "\u81ea\u5206\u6d41\u306e\u52c9\u5f37\u65b9\u6cd5\u3092\u898b\u3064\u3051\u308b\u3053\u3068\u304c\u5927\u5207\u3060\u3002", "It is important to find a study method that works for you."),
        ("\u6e08", "\u652f\u6255\u3044\u6e08\u307f\u306e\u5370\u3092\u78ba\u8a8d\u3057\u3066\u304b\u3089\u3001\u5546\u54c1\u3092\u53d7\u3051\u53d6\u3063\u305f\u3002", "After checking the paid stamp, I received the item."),
        ("\u6e2f", "\u8239\u306f\u671d\u4e03\u6642\u306b\u6a2a\u6d5c\u6e2f\u3092\u51fa\u822a\u3057\u305f\u3002", "The ship left Yokohama Port at seven in the morning."),
        ("\u6ef4", "\u76ee\u306b\u76ee\u85ac\u3092\u4e00\u6ef4\u3055\u3057\u307e\u3057\u305f\u3002", "I put one drop of eye medicine in my eye."),
        ("\u70b9", "\u82f1\u8a9e\u306e\u8a66\u9a13\u3067\u4e5d\u5341\u70b9\u3092\u53d6\u308a\u307e\u3057\u305f\u3002", "I scored ninety points on the English test."),
        ("\u7523", "\u3053\u306e\u5e97\u306f\u5730\u5143\u7523\u306e\u91ce\u83dc\u3092\u4f7f\u3063\u3066\u3044\u307e\u3059\u3002", "This shop uses locally grown vegetables."),
        ("\u756a\u76ee", "\u30c1\u30fc\u30e0\u306f\u4eca\u5b63\u4e8c\u756a\u76ee\u306e\u512a\u52dd\u3092\u76ee\u6307\u3057\u3066\u3044\u308b\u3002", "The team is aiming for its second championship this season."),
        ("\u7573", "\u516d\u7573\u306e\u90e8\u5c4b\u306b\u673a\u3068\u30d9\u30c3\u30c9\u3092\u7f6e\u3044\u305f\u3002", "I put a desk and bed in the six-tatami room."),
        ("\u75c5", "\u75c5\u6c17\u3092\u4e88\u9632\u3059\u308b\u306b\u306f\u3001\u624b\u6d17\u3044\u304c\u5927\u5207\u3067\u3059\u3002", "Handwashing is important for preventing illness."),
        ("\u767a", "\u591c\u7a7a\u306b\u82b1\u706b\u304c\u4e09\u767a\u4e0a\u304c\u3063\u305f\u3002", "Three fireworks burst in the night sky."),
        ("\u7701", "\u74b0\u5883\u7701\u306f\u65b0\u3057\u3044\u8abf\u67fb\u7d50\u679c\u3092\u516c\u8868\u3057\u305f\u3002", "The Environment Ministry released new survey results."),
        ("\u7740", "\u5f7c\u306f\u63a5\u6226\u306e\u30ec\u30fc\u30b9\u3067\u4e09\u7740\u306b\u5165\u3063\u305f\u3002", "He finished third in a close race."),
        ("\u793e", "\u4e09\u793e\u304c\u5171\u540c\u3067\u5546\u54c1\u306e\u8abf\u67fb\u3092\u884c\u3063\u305f\u3002", "Three companies jointly surveyed the product."),
        ("\u79d1", "\u75c7\u72b6\u3092\u8abf\u3079\u305f\u7d50\u679c\u3001\u5185\u79d1\u3092\u53d7\u8a3a\u3059\u308b\u3053\u3068\u306b\u306a\u3063\u305f\u3002", "After assessing the symptoms, I was told to visit internal medicine."),
        ("\u7b49", "\u4e21\u8005\u306f\u540c\u7b49\u306e\u8cac\u4efb\u3092\u8ca0\u3063\u3066\u3044\u308b\u3002", "Both parties bear equal responsibility."),
        ("\u7cfb", "\u7406\u7cfb\u306e\u5b66\u751f\u5411\u3051\u306b\u5c31\u8077\u8aac\u660e\u4f1a\u304c\u958b\u304b\u308c\u305f\u3002", "A career session was held for science students."),
        ("\u7d19", "\u548c\u7d19\u3092\u4f7f\u3063\u3066\u5b63\u7bc0\u306e\u6311\u3057\u7d75\u3092\u4f5c\u3063\u305f\u3002", "We made a seasonal illustration using Japanese paper."),
        ("\u7fbd", "\u6c60\u306e\u307b\u3068\u308a\u3067\u9ce5\u304c\u4e8c\u7fbd\u98db\u3073\u7acb\u3063\u305f\u3002", "Two birds took flight beside the pond."),
        ("\u8005", "\u8abf\u67fb\u306e\u95a2\u4fc2\u8005\u304b\u3089\u8a73\u3057\u3044\u8aac\u660e\u3092\u805e\u3044\u305f\u3002", "I heard a detailed explanation from the people involved in the survey."),
        ("\u8239", "\u5927\u304d\u306a\u8239\u304c\u4e8c\u96bb\u3001\u6e2f\u306b\u5165\u3063\u3066\u304d\u305f\u3002", "Two large ships entered the harbor."),
        ("\u8272", "\u685c\u8272\u306e\u7740\u7269\u304c\u6625\u306e\u796d\u308a\u306b\u3088\u304f\u4f3c\u5408\u3063\u3066\u3044\u305f\u3002", "The cherry-blossom-colored kimono suited the spring festival."),
        ("\u884c", "\u9280\u884c\u306e\u7a93\u53e3\u3067\u4f4f\u6240\u5909\u66f4\u306e\u624b\u7d9a\u304d\u3092\u3057\u305f\u3002", "I changed my address at the bank counter."),
        ("\u8ad6", "\u5341\u5206\u306b\u8b70\u8ad6\u3057\u305f\u7d50\u679c\u3001\u5168\u54e1\u304c\u7d0d\u5f97\u3067\u304d\u308b\u7d50\u8ad6\u306b\u81f3\u3063\u305f\u3002", "After a thorough discussion, we reached a conclusion everyone could accept."),
        ("\u8cbb", "\u51fa\u5f35\u8cbb\u306e\u7533\u8acb\u66f8\u3092\u4e0a\u53f8\u306b\u63d0\u51fa\u3057\u307e\u3057\u305f\u3002", "I submitted my business-trip expense form to my manager."),
        ("\u8db3", "\u767b\u5c71\u306e\u524d\u306b\u3001\u81ea\u5206\u306b\u5408\u3046\u9774\u3092\u4e00\u8db3\u8cb7\u3063\u305f\u3002", "Before hiking, I bought a pair of shoes that fit me."),
        ("\u8eca", "\u901a\u52e4\u6642\u9593\u306b\u306f\u96fb\u8eca\u304c\u6df7\u307f\u5408\u3044\u307e\u3059\u3002", "The trains are crowded during commuting hours."),
        ("\u901a", "\u7533\u8acb\u66f8\u3092\u4e8c\u901a\u4f5c\u6210\u3057\u3001\u7a93\u53e3\u306b\u63d0\u51fa\u3057\u305f\u3002", "I made two copies of the application and submitted them at the counter."),
        ("\u9053", "\u901a\u5b66\u9053\u306e\u5b89\u5168\u5bfe\u7b56\u304c\u5f37\u5316\u3055\u308c\u305f\u3002", "Safety measures on the school route were strengthened."),
        ("\u90e8", "\u6a5f\u68b0\u306e\u4e00\u90e8\u304c\u6545\u969c\u3057\u305f\u305f\u3081\u3001\u4f5c\u696d\u3092\u4e00\u6642\u4e2d\u65ad\u3057\u305f\u3002", "Work was temporarily suspended because part of the machine had broken."),
        ("\u9152", "\u5730\u5143\u306e\u65e5\u672c\u9152\u3092\u98f2\u307f\u6bd4\u3079\u3066\u3001\u597d\u307f\u306e\u5473\u3092\u898b\u3064\u3051\u305f\u3002", "I sampled local sake and found the flavor I like."),
        ("\u9577", "\u652f\u5e97\u9577\u304c\u4f1a\u8b70\u306e\u9032\u884c\u3092\u62c5\u5f53\u3057\u305f\u3002", "The branch manager led the meeting."),
        ("\u9593", "\u4f1a\u8b70\u306e\u9593\u306f\u96fb\u8a71\u3092\u5207\u3063\u3066\u304a\u3044\u3066\u304f\u3060\u3055\u3044\u3002", "Please turn off your phone during the meeting."),
        ("\u96c6", "\u65c5\u884c\u306e\u5199\u771f\u96c6\u3092\u4f5c\u308b\u305f\u3081\u3001\u53cb\u4eba\u304b\u3089\u5199\u771f\u3092\u96c6\u3081\u305f\u3002", "I collected photos from friends to make a travel album."),
        ("\u9818", "\u9818\u57df\u306e\u5b89\u5168\u3092\u78ba\u4fdd\u3059\u308b\u305f\u3081\u3001\u95a2\u4fc2\u56fd\u304c\u5354\u8b70\u3057\u305f\u3002", "Neighboring countries discussed how to secure the territory."),
        ("\u982d", "\u8fb2\u5bb6\u306f\u4e73\u725b\u3092\u4e8c\u5341\u982d\u80b2\u3066\u3066\u3044\u308b\u3002", "The farmer raises twenty dairy cows."),
        ("\u98a8", "\u65b0\u3057\u3044\u5bbf\u306f\u4f1d\u7d71\u7684\u306a\u548c\u98a8\u306e\u5efa\u7269\u3067\u3059\u3002", "The new inn is a traditional Japanese-style building."),
        ("\u9928", "\u7f8e\u8853\u9928\u3067\u958b\u50ac\u4e2d\u306e\u7279\u5225\u5c55\u3092\u898b\u5b66\u3057\u305f\u3002", "I viewed the special exhibition at the art museum."),
    ])
    endings.extend([
        ("\u5149", "\u591c\u306e\u6708\u5149\u304c\u9759\u304b\u306a\u6e56\u9762\u3092\u7167\u3089\u3057\u3066\u3044\u305f\u3002", "Moonlight illuminated the quiet surface of the lake."),
        ("\u5272", "\u539f\u6599\u4fa1\u306e\u4e0a\u6607\u3067\u3001\u6765\u6708\u304b\u3089\u6599\u91d1\u304c\u4e00\u5272\u5897\u3057\u306b\u306a\u308b\u3002", "Because material costs have risen, fees will increase by ten percent next month."),
        ("\u5408\u305b", "\u7533\u8fbc\u8005\u3092\u5408\u308f\u305b\u308b\u3068\u3001\u4eca\u56de\u306f\u767e\u4eba\u306b\u306a\u308b\u3002", "There are one hundred applicants in total this time."),
        ("\u5473", "\u3053\u306e\u30b9\u30fc\u30d7\u306b\u306f\u3001\u307b\u306e\u304b\u306a\u7518\u307f\u304c\u3042\u308b\u3002", "This soup has a subtle sweetness."),
        ("\u55b6", "\u4e21\u89aa\u306f\u5730\u5143\u3067\u5c0f\u3055\u306a\u81ea\u55b6\u696d\u3092\u55b6\u3093\u3067\u3044\u308b\u3002", "My parents run a small business locally."),
        ("\u5897\u3057", "\u539f\u6599\u306e\u5024\u4e0a\u304c\u308a\u3067\u3001\u4f1a\u8cbb\u306f\u524d\u5e74\u3088\u308a\u4e00\u5272\u5897\u3057\u3068\u306a\u3063\u305f\u3002", "The membership fee is ten percent higher than last year due to rising material costs."),
        ("\u5b22", "\u304a\u5b22\u3055\u3093\u304c\u53d7\u4ed8\u3067\u6765\u5ba2\u306e\u540d\u524d\u3092\u78ba\u8a8d\u3057\u305f\u3002", "The young woman at reception checked the visitors' names."),
        ("\u5b97", "\u3053\u306e\u5730\u57df\u306b\u306f\u7570\u306a\u308b\u5b97\u6d3e\u306e\u5bfa\u304c\u5171\u5b58\u3057\u3066\u3044\u308b\u3002", "Temples of different Buddhist sects coexist in this area."),
        ("\u5bc4\u308a", "\u5915\u65b9\u306b\u306f\u5317\u5bc4\u308a\u306e\u98a8\u304c\u5f37\u307e\u308b\u4e88\u5831\u3067\u3059\u3002", "A northerly wind is expected to strengthen toward evening."),
        ("\u5fc3", "\u89aa\u5fc3\u306b\u652f\u3048\u3089\u308c\u3066\u3001\u5f7c\u306f\u65b0\u3057\u3044\u4e8b\u696d\u306b\u6311\u6226\u3057\u305f\u3002", "Encouraged by his parents' care, he took on a new business venture."),
        ("\u639b\u3051", "\u4e09\u4eba\u639b\u3051\u306e\u30bd\u30d5\u30a1\u3092\u30ea\u30d3\u30f3\u30b0\u306b\u7f6e\u3044\u305f\u3002", "I put a three-seat sofa in the living room."),
        ("\u6cb9", "\u91ce\u83dc\u3092\u3054\u307e\u6cb9\u3067\u7092\u3081\u3066\u3001\u5869\u3067\u5473\u3092\u6574\u3048\u305f\u3002", "I stir-fried the vegetables in sesame oil and seasoned them with salt."),
        ("\u6d3e", "\u4f1a\u8b70\u3067\u306f\u614e\u91cd\u6d3e\u3068\u7a4d\u6975\u6d3e\u3067\u610f\u898b\u304c\u5206\u304b\u308c\u305f\u3002", "Opinions at the meeting split between cautious and proactive groups."),
        ("\u72c2", "\u5144\u306f\u6620\u753b\u72c2\u3067\u3001\u6bce\u9031\u65b0\u4f5c\u3092\u898b\u306b\u884c\u304f\u3002", "My older brother is a film fanatic and goes to see new releases every week."),
        ("\u753b", "\u753b\u6570\u306e\u591a\u3044\u6f22\u5b57\u306f\u66f8\u304f\u306e\u306b\u6642\u9593\u304c\u304b\u304b\u308b\u3002", "Kanji with many strokes take time to write."),
        ("\u754c", "\u653f\u754c\u3067\u306f\u4e88\u7b97\u3092\u3081\u3050\u308b\u8b70\u8ad6\u304c\u7d9a\u3044\u3066\u3044\u308b\u3002", "Debate over the budget continues in political circles."),
        ("\u75c7", "\u82b1\u7c89\u75c7\u306e\u75c7\u72b6\u3092\u6291\u3048\u308b\u85ac\u3092\u533b\u5e2b\u306b\u51e6\u65b9\u3057\u3066\u3082\u3089\u3063\u305f\u3002", "My doctor prescribed medicine to ease my hay fever symptoms."),
        ("\u7f72", "\u4e8b\u4ef6\u306e\u8a73\u7d30\u306f\u3001\u7ba1\u8f44\u306e\u8b66\u5bdf\u7f72\u304c\u8abf\u3079\u3066\u3044\u308b\u3002", "The local police department is investigating the details of the case."),
        ("\u8457", "\u3053\u306e\u672c\u306f\u8a00\u8a9e\u5b66\u8005\u306e\u4f50\u85e4\u6c0f\u8457\u3067\u3059\u3002", "This book was written by the linguist Mr. Sato."),
        ("\u85ac", "\u98a8\u90aa\u85ac\u3092\u98f2\u3093\u3067\u3001\u4eca\u65e5\u306f\u65e9\u3081\u306b\u4f11\u3093\u3060\u3002", "I took cold medicine and rested early today."),
        ("\u8857", "\u5546\u5e97\u8857\u306b\u65b0\u3057\u3044\u55ab\u8336\u5e97\u304c\u958b\u5e97\u3057\u305f\u3002", "A new caf? opened in the shopping district."),
    ])
    for ending, ja, en in endings:
        if w.endswith(ending):
            return ja, en
    if "\u4eba" in w and gloss.casefold().startswith("counter"):
        return "\u6559\u5ba4\u306b\u5b66\u751f\u304c\u4e09\u4eba\u3044\u307e\u3059\u3002", "There are three students in the classroom."
    if "\u304b\u6708" in w or "\u30f6\u6708" in w:
        return "\u65e5\u672c\u306b\u4e09\u304b\u6708\u9593\u4f4f\u307f\u307e\u3057\u305f\u3002", "I lived in Japan for three months."
    if "\u3055\u3093" in w or "\u3061\u3083\u3093" in w or "\u304f\u3093" in w:
        return "\u7530\u4e2d\u3055\u3093\u306f\u4eca\u65e5\u4f11\u307f\u3067\u3059\u3002", "Mr. Tanaka is off today."
    if "\u4ee3" in w and gloss.startswith("~"):
        return "\u6bce\u6708\u306e\u96fb\u6c17\u4ee3\u306f\u4e00\u4e07\u5186\u304f\u3089\u3044\u3067\u3059\u3002", "The monthly electricity bill is about ten thousand yen."
    if w.startswith(("\u4e0d", "\u7121", "\u672a", "\u975e")):
        return "\u305d\u306e\u8a08\u753b\u306f\u73fe\u5b9f\u7684\u3067\u306f\u306a\u3044\u3068\u601d\u3044\u307e\u3059\u3002", "I do not think that plan is realistic."
    # Use a different concrete conversational context at each level for uncommon affixes.
    fallbacks = {
        "N5": ("\u4f11\u307f\u306e\u65e5\u306b\u53cb\u9054\u3068\u516c\u5712\u3078\u884c\u304d\u307e\u3057\u305f\u3002", "I went to the park with a friend on my day off."),
        "N4": ("\u65c5\u884c\u306e\u524d\u306b\u3001\u5fc5\u8981\u306a\u3082\u306e\u3092\u5168\u90e8\u6e96\u5099\u3057\u307e\u3057\u305f\u3002", "I prepared everything I needed before the trip."),
        "N3": ("\u4f1a\u8b70\u306e\u3042\u3068\u3001\u7d50\u679c\u3092\u30ce\u30fc\u30c8\u306b\u307e\u3068\u3081\u307e\u3057\u305f\u3002", "After the meeting, I summarized the results in my notes."),
        "N2": ("\u4e88\u5b9a\u304c\u5909\u308f\u3063\u305f\u305f\u3081\u3001\u65c5\u884c\u306e\u8a08\u753b\u3092\u898b\u76f4\u3057\u307e\u3057\u305f\u3002", "Because the schedule changed, I reviewed the travel plans."),
        "N1": ("\u8abf\u67fb\u7d50\u679c\u3092\u8e0f\u307e\u3048\u3066\u3001\u4eca\u5f8c\u306e\u65b9\u91dd\u3092\u614e\u91cd\u306b\u6c7a\u3081\u308b\u5fc5\u8981\u304c\u3042\u308b\u3002", "Based on the findings, we need to decide the next policy carefully."),
    }
    return fallbacks[level]


def sentence_for(level: str, pos: str, word: dict) -> tuple[str, str]:
    if pos == "expression":
        return expression_sentence(level, word["word"], word["gloss"])
    target = "\u0000WORD\u0000"
    if pos == "verb":
        frames = {
            "N5": ("\u6bce\u65e5\u3001\u79c1\u306f", "\u3053\u3068\u304c\u3067\u304d\u307e\u3059\u3002", "I can"),
            "N4": ("\u4f11\u307f\u306e\u65e5\u306f\u3001\u5bb6\u65cf\u3068", "\u3053\u3068\u306b\u3057\u3066\u3044\u307e\u3059\u3002", "On my days off, I make a point of"),
            "N3": ("\u6642\u9593\u304c\u3042\u308c\u3070\u3001\u5e30\u308b\u524d\u306b", "\u3088\u3046\u306b\u3057\u3066\u3044\u307e\u3059\u3002", "When I have time, I make sure to"),
            "N2": ("\u4eca\u5f8c\u306f\u3001\u5fc5\u8981\u306b\u5fdc\u3058\u3066", "\u65b9\u91dd\u3067\u3059\u3002", "From now on, the policy is to"),
            "N1": ("\u72b6\u6cc1\u3092\u898b\u6975\u3081\u305f\u4e0a\u3067", "\u65b9\u91dd\u3092\u6c7a\u5b9a\u3059\u308b\u3002", "After assessing the situation, we will decide to"),
        }
        prefix, suffix, english_prefix = frames[level]
        gloss = word["gloss_verb"]
        return prefix + target + suffix, f"{english_prefix} {gloss}."
    if pos in {"adj-i", "adj-na"}:
        frames = {
            "N5": ("\u3053\u306e\u98df\u3079\u7269\u306f\u3068\u3066\u3082", "\u3067\u3059\u3002", "This food is very"),
            "N4": ("\u3053\u306e\u753a\u306f\u4f4f\u307f\u3084\u3059\u304f\u3066", "\u3067\u3059\u3002", "This town is easy to live in and"),
            "N3": ("\u5b9f\u969b\u306b\u898b\u308b\u3068\u3001\u60f3\u50cf\u3057\u3066\u3044\u305f\u3088\u308a", "\u3067\u3057\u305f\u3002", "In reality, it was more"),
            "N2": ("\u8abf\u67fb\u306e\u7d50\u679c\u306f\u3001\u4e88\u60f3\u4ee5\u4e0a\u306b", "\u3053\u3068\u304c\u5206\u304b\u308a\u307e\u3057\u305f\u3002", "The findings showed that it was more"),
            "N1": ("\u5f53\u521d\u306e\u898b\u8fbc\u307f\u3068\u6bd4\u3079\u3066\u3001\u6700\u7d42\u7684\u306a\u7d50\u679c\u306f", "\u3068\u8a00\u3048\u308b\u3002", "Compared with the initial estimate, the final result can be described as"),
        }
        prefix, suffix, english_prefix = frames[level]
        return prefix + target + suffix, f"{english_prefix} {word['gloss_adj']}."
    if pos == "adverb":
        frames = {
            "N5": ("\u3086\u3063\u304f\u308a", "\u6b69\u3044\u3066\u304f\u3060\u3055\u3044\u3002", "Please walk slowly."),
            "N4": ("\u8aac\u660e\u66f8\u3092", "\u3088\u304f\u8aad\u3093\u3067\u304b\u3089\u4f7f\u3063\u3066\u304f\u3060\u3055\u3044\u3002", "Please read the instructions carefully before using it."),
            "N3": ("\u5f7c\u306f\u554f\u984c\u306b", "\u5bfe\u5fdc\u3057\u307e\u3057\u305f\u3002", "He responded to the problem"),
            "N2": ("\u5f7c\u306f\u76f8\u624b\u306e\u8aac\u660e\u3092", "\u805e\u3044\u305f\u4e0a\u3067\u3001\u5224\u65ad\u3057\u305f\u3002", "He made a decision after listening to the other person's explanation"),
            "N1": ("\u3053\u306e\u554f\u984c\u306f", "\u691c\u8a0e\u3059\u308b\u5fc5\u8981\u304c\u3042\u308b\u3002", "This issue needs to be considered"),
        }
        prefix, suffix, english = frames[level]
        return prefix + target + suffix, f"{english} {word['gloss_adv']}."
    if pos == "interjection":
        return "\u300c" + target + "\u300d\u3068\u5f7c\u306f\u8a00\u3044\u307e\u3057\u305f\u3002", f'He said, "{word["gloss"]}."'
    frames = {
        "N5": ("\u304d\u3087\u3046\u3001\u5e97\u3067", "\u3092\u8cb7\u3044\u307e\u3057\u305f\u3002", "Today, I bought"),
        "N4": ("\u4f11\u307f\u306e\u65e5\u306b\u3001\u53cb\u9054\u3068", "\u306b\u3064\u3044\u3066\u8a71\u3057\u307e\u3057\u305f\u3002", "On my day off, I talked with a friend about"),
        "N3": ("\u6388\u696d\u3067", "\u306b\u3064\u3044\u3066\u5b66\u3073\u307e\u3057\u305f\u3002", "In class, we learned about"),
        "N2": ("\u305d\u306e", "\u306b\u3064\u3044\u3066\u3001\u8a73\u3057\u304f\u8aac\u660e\u3092\u53d7\u3051\u307e\u3057\u305f\u3002", "I received a detailed explanation about that"),
        "N1": ("\u8abf\u67fb\u306e\u7d50\u679c\u3001", "\u304c\u4eca\u5f8c\u306e\u5927\u304d\u306a\u8ab2\u984c\u3068\u3057\u3066\u6d6e\u304b\u3073\u4e0a\u304c\u3063\u305f\u3002", "The findings showed that"),
    }
    prefix, suffix, english = frames[level]
    return prefix + target + suffix, f"{english} {word['gloss']}."


def converted_markup(value: str) -> str:
    return ruby_markup(value)


def make_example(entry: dict) -> dict:
    source_examples = entry.get("open_examples", [])
    for example in source_examples:
        ja = str(example.get("ja", "")).strip()
        english = str(example.get("en", "")).strip()
        if not ja or not english:
            continue
        furi = str(example.get("furigana", "")).strip()
        if not furi or not all_kanji_annotated(furi):
            furi = converted_markup(ja)
        if all_kanji_annotated(furi) and strip_ruby(furi) == ja:
            return {
                "ja": ja,
                "furigana": furi,
                "en": english,
                "source": f"OpenJLPT/Tatoeba:{example.get('tatoeba_id', 'unknown')}",
                "generated": False,
            }

    meanings = entry.get("meanings", [])
    gloss = str(meanings[0] if meanings else "this word").strip().strip(".;")
    gloss = re.sub(r"\s+", " ", gloss)
    gloss_verb = gloss[3:].strip() if gloss.casefold().startswith("to ") else gloss
    if not gloss_verb:
        gloss_verb = gloss
    gloss_adj = gloss
    gloss_adv = gloss
    english_source = {"gloss": gloss, "gloss_verb": gloss_verb, "gloss_adj": gloss_adj, "gloss_adv": gloss_adv}
    pos = fallback_pos(entry["word"], entry["reading"], gloss, entry.get("pos", []))
    template, english = sentence_for(entry["level"], pos, {**english_source, "word": entry["word"]})
    if pos == "expression":
        furi = converted_markup(template)
    else:
        prefix, _, suffix = template.partition("\u0000WORD\u0000")
        marked_word = entry["wordFurigana"]
        furi = converted_markup(prefix) + marked_word + converted_markup(suffix)
    ja = strip_ruby(furi)
    if not all_kanji_annotated(furi):
        fallback_text = template if pos == "expression" else prefix + entry["word"] + suffix
        furi = converted_markup(fallback_text)
        ja = strip_ruby(furi)
    if not all_kanji_annotated(furi):
        raise ValueError(
            f"Could not add furigana to generated sentence: {entry['word'].encode('unicode_escape').decode()}: "
            f"{furi.encode('unicode_escape').decode()}"
        )
    return {
        "ja": ja,
        "furigana": furi,
        "en": english,
        "source": "Original example generated for this app",
        "generated": True,
    }


def read_local_sources() -> list[dict]:
    records: list[dict] = []
    csv_dir = LOCAL_DIR / "csv"
    for number in range(1, 6):
        path = csv_dir / f"n{number}.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                records.append(
                    {
                        "level": f"N{number}",
                        "word": row.get("expression", ""),
                        "reading": row.get("reading", ""),
                        "meaning": row.get("meaning", ""),
                        "source": f"csv:{path.name}",
                        "tags": row.get("tags", ""),
                    }
                )
    topic_dir = LOCAL_DIR / "topics"
    for number in range(3, 6):
        level = f"N{number}"
        for suffix in ("topics", "topics_filtered"):
            path = topic_dir / f"n{number}_{suffix}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            for topic, words in data.get("topics", {}).items():
                for row in words:
                    records.append(
                        {
                            "level": level,
                            "word": row.get("expression", ""),
                            "reading": row.get("reading", ""),
                            "meaning": row.get("meaning", ""),
                            "source": f"topic:{path.name}#{topic}",
                            "tags": row.get("tags", ""),
                        }
                    )
        slim_path = topic_dir / f"n{number}_expressions_slim.json"
        slim = json.loads(slim_path.read_text(encoding="utf-8"))
        topic_words = {
            normalize(row.get("expression", "")): row
            for suffix in ("topics", "topics_filtered")
            for words in json.loads((topic_dir / f"n{number}_{suffix}.json").read_text(encoding="utf-8")).get("topics", {}).values()
            for row in words
        }
        for topic, expressions in slim.get("topics", {}).items():
            for expression in expressions:
                if normalize(expression) not in topic_words:
                    raise ValueError(f"Slim topic expression is missing its full word: {slim_path.name}: {expression}")
                row = topic_words[normalize(expression)]
                records.append(
                    {
                        "level": level,
                        "word": row.get("expression", ""),
                        "reading": row.get("reading", ""),
                        "meaning": row.get("meaning", ""),
                        "source": f"topic-slim:{slim_path.name}#{topic}",
                        "tags": row.get("tags", ""),
                    }
                )
    pdf_rows = json.loads(PDF_JSON.read_text(encoding="utf-8"))
    records.extend(pdf_rows)
    return [
        row
        for row in records
        if not re.fullmatch(r"[\uFF08(][\u3400-\u9fff]{1,3}(?:[\u3002\u30FB\u3001][\u3400-\u9fff]{1,3})*[\uFF09)]", normalize(row.get("word", "")))
    ]


def merge_data() -> dict[str, list[dict]]:
    open_entries: dict[str, dict] = {}
    by_canonical_key: dict[tuple[str, str], str] = {}
    for number in range(1, 6):
        path = OPEN_DIR / f"n{number}.json"
        for source in json.loads(path.read_text(encoding="utf-8")):
            entry = {
                "id": source["id"],
                "word": source["word"],
                "reading": source["reading"],
                "meanings": list(source.get("meanings", [])),
                "level_sources": {source["level"]},
                "sources": {f"OpenJLPT:{source['id']}"},
                "aliases": set(source.get("other_forms", [])),
                "pos": list(source.get("pos", [])),
                "open_examples": list(source.get("examples", [])),
            }
            key = key_for(entry["word"], entry["reading"])
            if key in by_canonical_key:
                existing = open_entries[by_canonical_key[key]]
                existing["level_sources"].add(source["level"])
                existing["sources"].add(f"OpenJLPT:{source['id']}")
                for meaning in entry["meanings"]:
                    merge_unique(existing["meanings"], meaning)
                existing["open_examples"].extend(entry["open_examples"])
            else:
                open_entries[source["id"]] = entry
                by_canonical_key[key] = source["id"]

    alias_index: dict[tuple[str, str], list[str]] = defaultdict(list)
    reading_index: dict[str, list[str]] = defaultdict(list)
    for entry_id, entry in open_entries.items():
        readings = [entry["reading"]]
        for form in [entry["word"], *entry["aliases"]]:
            for reading in readings:
                key = key_for(form, reading)
                if entry_id not in alias_index[key]:
                    alias_index[key].append(entry_id)
        reading_index[normalize(entry["reading"])].append(entry_id)

    local_only: dict[tuple[str, str], dict] = {}
    for local in read_local_sources():
        word = normalize(local.get("word", ""))
        reading = normalize(local.get("reading", ""))
        if not word:
            continue
        if not reading or has_kanji(reading):
            reading = "".join(part["hira"] for part in KAKASI.convert(word)) or word
        key = key_for(word, reading)
        target_id = by_canonical_key.get(key)
        if target_id is None:
            choices = alias_index.get(key, [])
            if choices:
                target_id = min(choices, key=lambda value: (RANK[min(open_entries[value]["level_sources"], key=RANK.get)], value))
        if target_id is None and local.get("meaning") and not has_kanji(reading):
            local_tokens = meaning_tokens(local["meaning"])
            scored = []
            for candidate_id in reading_index.get(reading, []):
                candidate_tokens = meaning_tokens(" ".join(open_entries[candidate_id]["meanings"]))
                overlap = local_tokens & candidate_tokens
                union = local_tokens | candidate_tokens
                score = len(overlap) / max(len(union), 1)
                if score >= 0.45:
                    scored.append((score, candidate_id))
            if scored:
                scored.sort(key=lambda item: (-item[0], RANK[min(open_entries[item[1]]["level_sources"], key=RANK.get)], item[1]))
                if len(scored) == 1 or scored[0][0] > scored[1][0]:
                    target_id = scored[0][1]
        if target_id is not None:
            target = open_entries[target_id]
            target["level_sources"].add(local["level"])
            target["sources"].add(local["source"])
            if word != target["word"]:
                target["aliases"].add(word)
            merge_unique(target["meanings"], local.get("meaning", ""))
            continue
        record = local_only.setdefault(
            key,
            {
                "id": "local-" + hashlib.sha256((key[0] + "\0" + key[1]).encode("utf-8")).hexdigest()[:12],
                "word": word,
                "reading": reading,
                "meanings": [],
                "level_sources": set(),
                "sources": set(),
                "aliases": set(),
                "pos": [],
                "open_examples": [],
            },
        )
        record["level_sources"].add(local["level"])
        record["sources"].add(local["source"])
        merge_unique(record["meanings"], local.get("meaning", ""))

    all_records = list(open_entries.values()) + list(local_only.values())
    output: dict[str, list[dict]] = {level: [] for level in LEVELS}
    for source in all_records:
        if not source["meanings"]:
            raise ValueError(f"No English meaning found for {source['word']} ({source['reading']})")
        level = min(source["level_sources"], key=RANK.get)
        marked_word = word_markup(source["word"], source["reading"])
        record = {
            "id": source["id"],
            "level": level,
            "word": source["word"],
            "reading": source["reading"],
            "wordFurigana": marked_word,
            "english": source["meanings"][0],
            "meanings": source["meanings"],
            "pos": source["pos"],
            "aliases": sorted(source["aliases"]),
            "sourceLevels": sorted(source["level_sources"], key=RANK.get),
            "sources": sorted(source["sources"]),
            "example": None,
            "exampleSource": None,
        }
        record["example"] = make_example({**record, "open_examples": source["open_examples"]})
        record["exampleSource"] = record["example"]["source"]
        output[level].append(record)

    for level in LEVELS:
        output[level].sort(key=lambda item: (normalize(item["word"]), normalize(item["reading"]), item["id"]))
        path = DATA_DIR / "levels" / (level.lower() + ".json")
        path.write_text(json.dumps(output[level], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main() -> None:
    pdf_rows = extract_pdf_entries()
    print(f"Extracted {len(pdf_rows):,} vocabulary rows from five text-layer PDFs.")
    output = merge_data()
    total = sum(map(len, output.values()))
    generated = sum(entry["example"]["generated"] for entries in output.values() for entry in entries)
    for level in LEVELS:
        print(f"{level}: {len(output[level]):,} words")
    print(f"Total: {total:,} unique entries; {generated:,} examples created for missing source pairs.")


if __name__ == "__main__":
    main()
