#!/usr/bin/env python3
"""Shared prose rules for the write gates.

One place for the checks that apply to anything a person will read later: a
Linear update, a PR body. Each returns a reason string, or None when clean.
The gates decide what to do with it.
"""
import re

HINT = (
    "Rewrite it by hand, then retry. Write what a colleague would write. Say "
    "what changed and why it matters. No poetry, no announcing what comes "
    "next, no restating the obvious."
)

SPELLED = (
    r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    r"fifty|sixty|seventy|eighty|ninety|hundred|thousand|million)\b"
)

BANNED = (
    r"\b(delve|crucial|pivotal|tapestry|testament|underscores?|robust|seamless|"
    r"leverages?|utilizes?|fosters?|harnesses|resonates?|elevates?|embraces?|"
    r"transformative|multifaceted|intricate|vibrant|myriad|realm|beacon|"
    r"paving the way|it'?s important to note|in today'?s)\b"
)

ANNOUNCE = (
    r"(^|\n)\s*(next up|going forward|in conclusion|in summary|to summarize|"
    r"stay tuned|looking ahead)"
)

CHATBOT = (
    r"\b(great question|happy to|hope this helps|let me know|feel free|"
    r"certainly!|of course!)\b"
)


def strip_safe(text: str) -> str:
    """Blank out spans where a pattern is data rather than prose."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b\d{1,2}:\d{2}\b", " ", text)
    text = re.sub(r"[\w/.-]+\.(py|ts|tsx|md|json|sql|yml|yaml):\d+", " ", text)
    return text


def check(raw: str, *, digit_spam: bool = False) -> str | None:
    """Return the first problem found, or None."""
    text = strip_safe(raw)
    low = text.lower()

    hit = re.search(SPELLED, low)
    if hit:
        return (f"Spelled-out number: '{hit.group(0)}'. Numbers are digits. "
                "Write 26, not twenty-six.")

    if digit_spam:
        numbers = re.findall(r"(?<![\w.])\d[\d,.]*", text)
        words = re.findall(r"[A-Za-z']+", text)
        ratio = len(numbers) / len(words) if words else 0
        if len(numbers) > 4 or (len(numbers) >= 3 and ratio > 0.12):
            return (f"Digit spam: {len(numbers)} numbers in {len(words)} words. "
                    "Keep only figures that change what the reader would think "
                    "or do. Most sentences need none.")

    hit = re.search(BANNED, low)
    if hit:
        return f"AI vocabulary: '{hit.group(0)}'. Use the plain word."

    hit = re.search(ANNOUNCE, low)
    if hit:
        return (f"Announcement instead of a fact: '{hit.group(0).strip()}'. "
                "Report what is true now.")

    hit = re.search(CHATBOT, low)
    if hit:
        return f"Chatbot phrasing: '{hit.group(0)}'. Cut it."

    for ch, name in (("—", "em dash"), ("–", "en dash"),
                     ("’", "curly apostrophe"), ("“", "curly quote")):
        if ch in raw:
            return f"Contains a {name}. Use plain punctuation and straight quotes."

    if re.search(r"[\U0001F300-\U0001FAFF]", raw):
        return "Contains an emoji. Remove it."

    hit = re.search(r"(?<![:/])\b\w{4,}:\s+[a-z]", text)
    if hit:
        return (f"Colon used as a connector: '{hit.group(0).strip()}'. "
                "End the sentence and start a new one.")
    return None
