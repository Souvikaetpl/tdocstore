"""One normaliser, applied identically at index time and at verbatim-lookup time.

Verbatim matching fails on invisible differences: NBSP, soft hyphens, curly quotes, tabs,
zero-width chars. We keep the ORIGINAL text in `paragraphs.text` (evidence stays byte-exact)
and match on the normalised form, mapping offsets back to the original.
"""
from __future__ import annotations
import unicodedata

_MAP = {
    " ": " ",  # NBSP
    " ": " ", " ": " ",
    "­": "",   # soft hyphen
    "​": "", "‌": "", "‍": "", "﻿": "",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...",
    "\t": " ",
}


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    out = []
    for ch in s:
        out.append(_MAP.get(ch, ch))
    s = "".join(out)
    # collapse runs of whitespace to one space (offsets are recomputed via map_offsets when needed)
    return " ".join(s.split()).strip().lower()


def normalise_with_map(s: str) -> tuple[str, list[int]]:
    """Return (normalised, map) where map[i] = index in original of normalised char i.
    Whitespace runs collapse to a single space that maps to the first original whitespace char."""
    s2 = unicodedata.normalize("NFC", s)  # keep length-stable-ish; NFKC can change lengths, avoid here
    out: list[str] = []
    idx: list[int] = []
    prev_space = True  # strip leading
    for i, ch in enumerate(s2):
        rep = _MAP.get(ch, ch)
        if rep == "":
            continue
        for r in rep:
            if r.isspace():
                if prev_space:
                    continue
                out.append(" ")
                idx.append(i)
                prev_space = True
            else:
                out.append(r.lower())
                idx.append(i)
                prev_space = False
    # strip trailing space
    while out and out[-1] == " ":
        out.pop(); idx.pop()
    return "".join(out), idx
