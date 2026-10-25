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
KANJI_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
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


def ruby_markup(value: str) -> str:
    """Create {base|reading} markup, using the source dictionary reading if needed."""
    pieces = KAKASI.convert(value)
    markup = "".join(
        "{" + piece["orig"] + "|" + piece["hira"] + "}"
        if has_kanji(piece["orig"]) and piece["hira"]
        else piece["orig"]
        for piece in pieces
    )
    return markup or value


def strip_ruby(value: str) -> str:
    return FURIGANA_RE.sub(lambda match: match.group(1), value)


def all_kanji_annotated(value: str) -> bool:
    visible = strip_ruby(value)
    return [c for c in value if KANJI_RE.fullmatch(c)] == [c for c in visible if KANJI_RE.fullmatch(c)] and not has_kanji(
        FURIGANA_RE.sub("", value)
    )


def word_markup(word: str, reading: str) -> str:
    candidate = ruby_markup(word)
    candidate_reading = "".join(part["hira"] for part in KAKASI.convert(word))
    if (
        all_kanji_annotated(candidate)
        and normalize(hiragana(candidate_reading)) == normalize(hiragana(reading))
    ):
        return candidate
    return "{" + word + "|" + reading + "}" if has_kanji(word) else word


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


def sentence_for(level: str, pos: str, word: str) -> tuple[str, str]:
    if pos == "verb":
        frames = {
            "N5": ("わたしは", "。", "I"),
            "N4": ("きのう、わたしは", "。", "Yesterday, I"),
            "N3": ("時間があれば、わたしは", "。", "If I have time, I"),
            "N2": ("状況が整えば、わたしは", "。", "If the situation allows, I"),
            "N1": ("必要であれば、わたしは", "。", "If needed, I"),
        }
        prefix, suffix, english_prefix = frames[level]
        english = (english_prefix + " " + word["gloss_verb"]).strip()
        return prefix + "\u0000WORD\u0000" + suffix, english
    if pos in {"adj-i", "adj-na"}:
        frames = {
            "N5": ("これはとても", "です。", "This is very"),
            "N4": ("この町は", "です。", "This town is"),
            "N3": ("この町は思ったより", "です。", "This town is more"),
            "N2": ("実際の状況は予想以上に", "です。", "The actual situation is even more"),
            "N1": ("この結果は当初の見込みより", "といえます。", "This result can be described as more"),
        }
        prefix, suffix, english_prefix = frames[level]
        english = f"{english_prefix} {word['gloss_adj']}."
        return prefix + "\u0000WORD\u0000" + suffix, english
    if pos == "adverb":
        frames = {
            "N5": ("わたしは", "行きます。", "I go"),
            "N4": ("きょう、わたしは", "行きます。", "Today, I go"),
            "N3": ("かれは", "帰ってきました。", "He came back"),
            "N2": ("かれは", "対応しました。", "He responded"),
            "N1": ("この問題は", "検討する必要があります。", "This problem needs to be considered"),
        }
        prefix, suffix, english_prefix = frames[level]
        english = f"{english_prefix} {word['gloss_adv']}."
        return prefix + "\u0000WORD\u0000" + suffix, english
    if pos == "interjection":
        return "「\u0000WORD\u0000」とかれは言いました。", f'He said, “{word["gloss"]}.”'
    if pos == "expression":
        templates = {
            "N5": ("\u300c\u0000WORD\u0000\u300d\u3068\u3044\u3046\u3053\u3068\u3070\u3092\u304a\u307c\u3048\u307e\u3059\u3002", f'I learn the expression ?{word["gloss"]}.?'),
            "N4": ("\u300c\u0000WORD\u0000\u300d\u3068\u3044\u3046\u8868\u73fe\u3092\u4f7f\u3044\u307e\u3059\u3002", f'I use the expression ?{word["gloss"]}.?'),
            "N3": ("\u3053\u306e\u6587\u3067\u306f\u300c\u0000WORD\u0000\u300d\u3068\u3044\u3046\u8868\u73fe\u3092\u4f7f\u3044\u307e\u3057\u305f\u3002", f'I used the expression ?{word["gloss"]}? in this sentence.'),
            "N2": ("\u3053\u306e\u6587\u8108\u3067\u306f\u300c\u0000WORD\u0000\u300d\u3068\u3044\u3046\u8868\u73fe\u304c\u9069\u5207\u3067\u3059\u3002", f'In this context, the expression ?{word["gloss"]}? is appropriate.'),
            "N1": ("\u6587\u8108\u306b\u5fdc\u3058\u3066\u300c\u0000WORD\u0000\u300d\u3068\u3044\u3046\u8868\u73fe\u3092\u4f7f\u3044\u5206\u3051\u307e\u3059\u3002", f'I choose when to use the expression ?{word["gloss"]}? according to the context.'),
        }
        return templates[level]
    frames = {
        "N5": ("これは", "です。", "This is"),
        "N4": ("きょう、みんなで", "について話しました。", "Today, we talked about"),
        "N3": ("じゅぎょうで", "について学びました。", "We learned about"),
        "N2": ("\u0000WORD\u0000をめぐって、意見が分かれています。", "", "Opinions differ about"),
        "N1": ("\u0000WORD\u0000を踏まえ、今後の方針を決めます。", "", "Taking into account"),
    }
    prefix, suffix, english_prefix = frames[level]
    if "\u0000WORD\u0000" in prefix:
        if level == "N1":
            return prefix, f"Taking {word['gloss']} into account, we will decide our future policy."
        return prefix, f"{english_prefix} {word['gloss']}."
    return prefix + "\u0000WORD\u0000" + suffix, f"{english_prefix} {word['gloss']}."


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
    template, english = sentence_for(entry["level"], pos, english_source)
    prefix, _, suffix = template.partition("\u0000WORD\u0000")
    marked_word = entry["wordFurigana"]
    furi = converted_markup(prefix) + marked_word + converted_markup(suffix)
    ja = strip_ruby(furi)
    if not all_kanji_annotated(furi):
        furi = converted_markup(prefix + entry["word"] + suffix)
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
        if not re.fullmatch(r"[（(][\u3400-\u9fff]{1,3}[）)]", normalize(row.get("word", "")))
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
