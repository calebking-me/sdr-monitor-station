"""DCS (Digital-Coded Squelch / CDCSS / DPL) codes.

A DCS code word is a 23-bit Golay (23,12) code word sent continuously, least
significant bit first, at 134.4 bit/s as sub-audible NRZ FSK under the voice.
The 12 data bits are the 9-bit code (three octal digits) with the fixed bits
'100' above them; the 11 check bits come from the Golay generator 0xC75 using
Hank Wallace's systematic encoder, giving the word [check(11), data(12)].
For example DCS 023 encodes to 0x763813.

Because the code is cyclic and repeated with no frame sync, a receiver sees
every rotation of the word, and several octal codes describe the same bit
stream (e.g. 023, 340 and 766). Inverting the stream gives another code
(023 inverted = 047 normal). Codes are therefore matched by *class*: the set
of all 23-bit windows a stream can produce.

Polarity convention: 'N' (normal) means a 1 bit raises the carrier frequency;
'I' (inverted) is the complement. 'either' accepts both.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

import numpy as np

BIT_RATE = 134.4
WORD_BITS = 23
_MASK = (1 << WORD_BITS) - 1
_POLY = 0xC75

# The 104 standard codes (as listed by radio manufacturers and CHIRP).
STANDARD_CODES = (
    0o023, 0o025, 0o026, 0o031, 0o032, 0o036, 0o043, 0o047, 0o051, 0o053, 0o054, 0o065, 0o071,
    0o072, 0o073, 0o074, 0o114, 0o115, 0o116, 0o122, 0o125, 0o131, 0o132, 0o134, 0o143, 0o145,
    0o152, 0o155, 0o156, 0o162, 0o165, 0o172, 0o174, 0o205, 0o212, 0o223, 0o225, 0o226, 0o243,
    0o244, 0o245, 0o246, 0o251, 0o252, 0o255, 0o261, 0o263, 0o265, 0o266, 0o271, 0o274, 0o306,
    0o311, 0o315, 0o325, 0o331, 0o332, 0o343, 0o346, 0o351, 0o356, 0o364, 0o365, 0o371, 0o411,
    0o412, 0o413, 0o423, 0o431, 0o432, 0o445, 0o446, 0o452, 0o454, 0o455, 0o462, 0o464, 0o465,
    0o466, 0o503, 0o506, 0o516, 0o523, 0o526, 0o532, 0o546, 0o565, 0o606, 0o612, 0o624, 0o627,
    0o631, 0o632, 0o654, 0o662, 0o664, 0o703, 0o712, 0o723, 0o731, 0o732, 0o734, 0o743, 0o754,
)
POLARITIES = ("N", "I", "either")


def golay_word(code: int) -> int:
    """23-bit DCS word for a 9-bit code (Wallace's encoder, [check(11), data(12)])."""
    data = 0x800 | (code & 0o777)
    cw = data
    for _ in range(12):
        if cw & 1:
            cw ^= _POLY
        cw >>= 1
    return (cw << 12) | data


def _rotations(word: int) -> frozenset[int]:
    return frozenset(((word >> k) | (word << (WORD_BITS - k))) & _MASK for k in range(WORD_BITS))


def stream_bits(code: int, inverted: bool = False) -> np.ndarray:
    """One word of the transmitted bit stream (LSB first), as 0/1 uint8."""
    w = golay_word(code) ^ (_MASK if inverted else 0)
    return np.array([(w >> k) & 1 for k in range(WORD_BITS)], dtype=np.uint8)


@lru_cache(maxsize=1)
def _tables():
    """Pattern -> class lookup built from the standard codes in both polarities."""
    classes: dict[frozenset, int] = {}
    names: list[str] = []
    for inverted in (False, True):               # normal names take priority
        for code in STANDARD_CODES:
            pats = _rotations(golay_word(code) ^ (_MASK if inverted else 0))
            if pats not in classes:
                classes[pats] = len(names)
                names.append(f"{code:03o}{'I' if inverted else 'N'}")
    pattern_list = sorted((p, cid) for pats, cid in classes.items() for p in pats)
    patterns = np.array([p for p, _ in pattern_list], dtype=np.int64)
    pattern_class = np.array([c for _, c in pattern_list], dtype=np.int32)
    return classes, names, patterns, pattern_class


def lookup_arrays() -> tuple[np.ndarray, np.ndarray]:
    """(sorted 23-bit patterns, class id per pattern) for vectorised matching."""
    _, _, patterns, pattern_class = _tables()
    return patterns, pattern_class


def class_name(class_id: int) -> str:
    return _tables()[1][class_id]


def class_of(code: int, inverted: bool) -> Optional[int]:
    """Class id of a code/polarity, or None if it is not receivable as a standard code."""
    pats = _rotations(golay_word(code) ^ (_MASK if inverted else 0))
    return _tables()[0].get(pats)


@lru_cache(maxsize=512)
def accept_classes(code_text: str, polarity: str) -> frozenset[int]:
    """Classes that open the squelch of a channel set to `code_text` / `polarity`."""
    try:
        code = int(code_text, 8)
    except (TypeError, ValueError):
        return frozenset()
    out = set()
    if polarity in ("N", "either"):
        out.add(class_of(code, False))
    if polarity in ("I", "either"):
        out.add(class_of(code, True))
    out.discard(None)
    return frozenset(out)


def parse_code(text: str) -> tuple[Optional[str], Optional[str]]:
    """Parse '023', 'D023N', 'DCS 754I', '23' ... into ('023', 'N'/'I'/None).

    Returns (None, None) for empty/'off'. Raises ValueError for anything that is
    not an octal code that maps onto a standard DCS code.
    """
    s = str(text or "").strip().upper().replace(" ", "")
    if s in ("", "OFF", "NONE", "0", "NO"):
        return None, None
    if s.startswith("DCS"):
        s = s[3:]
    elif s.startswith("D"):
        s = s[1:]
    pol = None
    if s and s[-1] in "NIR":
        pol = "I" if s[-1] in "IR" else "N"
        s = s[:-1]
    if not s or not s.isdigit() or any(ch in "89" for ch in s) or len(s) > 3:
        raise ValueError(f"{text!r} is not a DCS code (three octal digits, e.g. 023)")
    code = int(s, 8)
    if class_of(code, False) is None and class_of(code, True) is None:
        raise ValueError(f"DCS {code:03o} is not a usable code")
    return f"{code:03o}", pol


def describe(code: Optional[str], polarity: str) -> str:
    if not code:
        return ""
    suffix = {"N": "N", "I": "I"}.get(polarity, " (either polarity)")
    return f"DCS {code}{suffix}"
