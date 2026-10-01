"""Deterministic safety backstops around the LLM.

This screen is NOT the primary emergency detector. It is a high-precision,
low-recall keyword backstop for the unambiguous cases, where we want a
guarantee that does not depend on the model. Subtle presentations (for
example stroke signs described without the word "stroke") are left to the
model, and the eval suite deliberately includes cases this regex misses,
so we can see that it misses them.
"""
from __future__ import annotations

import re

from .clinic import EMERGENCY_NUMBER

RED_FLAG_PATTERNS = {
    "chest_pain": r"\bchest (pain|pressure|tightness)\b|\bcrushing\b.*\bchest\b",
    "breathing": r"\b(can'?t|cannot|struggling to|hard to) breathe\b|\bshort(ness)? of breath\b",
    "stroke": r"\bstroke\b|\bface (is )?droop",
    "bleeding": r"\b(heavy|won'?t stop|uncontrolled) bleeding\b|\bbleeding (heavily|won'?t stop)\b",
    "suicide": r"\b(kill(ing)? myself|suicid\w*|end(ing)? my life|want to die)\b",
    "unconscious": r"\b(unconscious|passed out|not breathing|unresponsive)\b",
    "anaphylaxis": r"\b(throat|tongue) (is )?(swelling|closing)\b|\banaphyla\w*",
    "overdose": r"\boverdos\w*\b",
}

EMERGENCY_LINE = (f"If this could be an emergency, please call {EMERGENCY_NUMBER} now or go to the nearest "
                  f"emergency department. Don't wait for an appointment.")
CRISIS_LINE = "If you are thinking about harming yourself, please call or text 988 (Suicide & Crisis Lifeline) now."


def screen(text: str) -> str | None:
    t = text.lower()
    for name, pat in RED_FLAG_PATTERNS.items():
        if re.search(pat, t):
            return name
    return None


def guard_output(reply: str, red_flag: str | None, earlier_text: str = "") -> tuple[str, bool]:
    """When the backstop fired on this turn, make sure the reply carries emergency guidance.

    Returns (reply, modified). This is the last line of defence and is visible in the
    trace, so the eval can tell 'the model handled it' apart from 'the guard saved it'.
    """
    if not red_flag:
        return reply, False
    line = CRISIS_LINE if red_flag == "suicide" else EMERGENCY_LINE
    needed = "988" if red_flag == "suicide" else EMERGENCY_NUMBER
    if needed in reply or needed in earlier_text:
        return reply, False
    return (reply.rstrip() + "\n\n" + line).strip(), True
