#!/usr/bin/env python3
"""Check level assignment, provenance, completeness, and furigana in built data."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
LEVELS = ["N5", "N4", "N3", "N2", "N1"]
RANK = {level: index for index, level in enumerate(LEVELS)}
KANJI_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
RUBY_RE = re.compile(r"\{([^{}|]+)\|([^{}|]+)\}")


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "").strip()).replace("〜", "～")


def strip_ruby(value: str) -> str:
    return RUBY_RE.sub(lambda match: match.group(1), value)


def all_kanji_annotated(value: str) -> bool:
    return not KANJI_RE.search(RUBY_RE.sub("", value))


def main() -> None:
    all_ids: set[str] = set()
    all_words: set[tuple[str, str]] = set()
    totals = {level: 0 for level in LEVELS}
    generated = {level: 0 for level in LEVELS}
    problems: list[str] = []

    for level in LEVELS:
        path = APP_DIR / "data" / "levels" / (level.lower() + ".json")
        entries = json.loads(path.read_text(encoding="utf-8"))
        seen_ids: set[str] = set()
        for entry in entries:
            where = f"{path.name}:{entry.get('word', '?')}"
            if entry.get("level") != level:
                problems.append(f"{where}: stored in wrong level")
            if not entry.get("id") or entry["id"] in seen_ids or entry["id"] in all_ids:
                problems.append(f"{where}: missing or duplicate id {entry.get('id')}")
            seen_ids.add(entry.get("id", ""))
            all_ids.add(entry.get("id", ""))
            key = (normalize(entry.get("word", "")), normalize(entry.get("reading", "")))
            if not key[0] or not key[1]:
                problems.append(f"{where}: missing Japanese word or reading")
            if key in all_words:
                problems.append(f"{where}: duplicate spelling/reading across levels")
            all_words.add(key)
            if not entry.get("meanings") or not entry.get("english"):
                problems.append(f"{where}: missing English meaning")
            if not entry.get("sources") or not entry.get("sourceLevels"):
                problems.append(f"{where}: missing provenance")
            if entry.get("sourceLevels") and min(entry["sourceLevels"], key=RANK.get) != level:
                problems.append(f"{where}: not assigned to easiest level {entry['sourceLevels']}")
            word_furigana = entry.get("wordFurigana", "")
            if strip_ruby(word_furigana) != entry.get("word"):
                problems.append(f"{where}: headword furigana does not match word")
            if not all_kanji_annotated(word_furigana):
                problems.append(f"{where}: headword contains kanji without furigana")
            example = entry.get("example") or {}
            if not example.get("ja") or not example.get("en") or not example.get("furigana"):
                problems.append(f"{where}: missing Japanese/English example or furigana")
            elif strip_ruby(example["furigana"]) != example["ja"]:
                problems.append(f"{where}: example furigana does not match the Japanese sentence")
            elif not all_kanji_annotated(example["furigana"]):
                problems.append(f"{where}: example contains kanji without furigana")
            if example.get("generated"):
                generated[level] += 1
            totals[level] += 1

    if problems:
        for problem in problems[:100]:
            print("ERROR:", problem)
        raise SystemExit(f"Validation failed with {len(problems)} error(s).")

    print("Dataset validation passed.")
    for level in LEVELS:
        print(f"{level}: {totals[level]:,} words; {generated[level]:,} generated examples")
    print(f"Total: {sum(totals.values()):,} unique words")


if __name__ == "__main__":
    main()
