"""Lay dictated text out the way a person would type it: bullet points (or
short paragraphs) for long dictations, and a numbered list when the speaker
counted things off ("first… second…").

Done with plain rules, not the LLM: the 3B cleanup model ignored layout
instructions on 71 of 74 real dictations and reworded more when asked to
format. Rules are instant, use no memory, and never change the wording —
the only words ever removed are the spoken list markers themselves.
"""

import re

MAX_PARAGRAPH_WORDS = 40   # past this, break at the next sentence
MIN_PARAGRAPH_WORDS = 20   # don't break for a topic marker before this
SHORT_TEXT_WORDS = 45      # anything shorter stays on one line

_ABBREVIATIONS = ("mr.", "mrs.", "ms.", "dr.", "vs.", "etc.", "e.g.", "i.e.",
                  "st.", "no.")

_ORDINALS = {
    "first": 1, "firstly": 1, "one": 1,
    "second": 2, "secondly": 2, "two": 2,
    "third": 3, "thirdly": 3, "three": 3,
    "fourth": 4, "four": 4, "fifth": 5, "five": 5, "sixth": 6, "six": 6,
    "seventh": 7, "seven": 7, "eighth": 8, "eight": 8, "ninth": 9, "nine": 9,
    "tenth": 10, "ten": 10,
}
_LEAD_IN = r"(?:(?:okay|ok|so|and|also|then|now|alright|well)[,]?\s+)*"
_NOUN = (r"(?:thing|one|step|point|reel|video|question|issue|problem|part|"
         r"task|item|reason|option|idea|change|tip|rule)")
_LINK = r"(?:is that|is|was|would be|will be|being)"
# "First, …" / "The second one would be …" / "Number three: …" / "Five would be …"
_MARKER = re.compile(
    rf"^{_LEAD_IN}(?:the\s+)?(?:number\s+)?"
    rf"(?P<ord>{'|'.join(_ORDINALS)})"
    rf"(?:(?:\s+(?:of all|off|things? first))?(?:\s+{_NOUN})?(?:\s+{_LINK})?\s*[,:–—-]\s*"
    rf"|(?:\s+{_NOUN})?\s+{_LINK}\s+"
    rf"|\s+{_NOUN}\s*[,:]?\s+)",
    re.IGNORECASE)

# A sentence starting like this usually opens a new point.
_TOPIC_SHIFT = re.compile(
    r"^(?:and\s+)?(?:also|another thing|the other thing|one more thing|next|"
    r"now|okay|ok|alright|anyway|by the way|additionally|in addition|"
    r"moving on|on top of that|as for|regarding|finally|lastly|after that|"
    r"once (?:we're|we are|that's|that is|you're|you are) done|for context|"
    r"second(?:ly)?|third(?:ly)?)\b",
    re.IGNORECASE)
# …and one starting like this continues the previous sentence's thought.
_CONTINUATION = re.compile(
    r"^(?:and|but|because|so that|which|or)\b", re.IGNORECASE)
# A sentence that points back at the one before it ("Is that possible?",
# "That would help.") belongs in the same paragraph, as does a very short one.
_POINTS_BACK = re.compile(
    r"^(?:(?:is|was|does|did|would|could|can|will|isn't|doesn't|wouldn't)\s+)?"
    r"(?:that|this|it|those|these|they)\b", re.IGNORECASE)
_SHORT_FOLLOW_UP_WORDS = 4

# Bullet points: a long dictation is usually several separate asks.
POINTS_MIN_WORDS = 60      # shorter dictations stay one paragraph
POINT_MAX_WORDS = 32       # past this, a point ends at the next sentence
POINT_MIN_WORDS = 10       # don't end a point for a new topic before this
_DEPENDENT = re.compile(r"^(?:so that|because|which|that's why)\b", re.IGNORECASE)
BULLET = "• "              # looks the same in every app, unlike "-" or "*"
_GREETING = re.compile(
    r"^(?:hey|hi|hello|dear|yo|good (?:morning|afternoon|evening))\b",
    re.IGNORECASE)



def split_sentences(text: str) -> list[str]:
    parts = re.split(
        r"(?:(?<=[.!?])|(?<=[.!?][\"')\]]))\s+(?=[\"'(\[]*[A-Z0-9])",
        text.strip())
    sentences: list[str] = []
    for part in parts:
        if sentences and sentences[-1].lower().endswith(_ABBREVIATIONS):
            sentences[-1] += " " + part
        else:
            sentences.append(part)
    return [s for s in sentences if s]


def _words(sentence: str) -> int:
    return len(sentence.split())


def _strip_marker(sentence: str) -> str | None:
    """The sentence without its spoken list marker, or None if what is left
    is too short to stand as a list item."""
    rest = _MARKER.sub("", sentence, count=1).strip()
    if _words(rest) < 2:
        return None
    return rest[0].upper() + rest[1:]


def _find_list(sentences: list[str]) -> tuple[int, list[list[str]], int] | None:
    """Find a counted-off run: (index of first item, items, index after it).

    Only a run that starts at "first/one" and counts up without gaps
    qualifies, so a stray "second" or "one" in normal prose is left alone.
    """
    numbers = []
    for sentence in sentences:
        m = _MARKER.match(sentence)
        numbers.append(_ORDINALS[m.group("ord").lower()] if m else 0)
    for start, number in enumerate(numbers):
        if number != 1:
            continue
        starts, expected = [start], 2
        for i in range(start + 1, len(numbers)):
            if numbers[i] == expected:
                starts.append(i)
                expected += 1
            elif numbers[i]:
                break  # out of order: stop the run here
        if len(starts) < 2:
            continue
        items = [sentences[a:b] for a, b in zip(starts, starts[1:])]
        # The last item has no next marker to end it; give it as many
        # sentences as the longest item before it, at most.
        longest = max(len(item) for item in items)
        end = min(starts[-1] + longest, len(sentences))
        for i in range(starts[-1] + 1, end):
            if _TOPIC_SHIFT.match(sentences[i]):
                end = i  # "Also, …" after the last item starts a new point
                break
        items.append(sentences[starts[-1]:end])
        stripped = []
        for item in items:
            head = _strip_marker(item[0])
            if head is None:
                break
            stripped.append([head] + item[1:])
        else:
            return start, stripped, end
    return None


def _paragraphs(sentences: list[str]) -> list[str]:
    total = sum(_words(s) for s in sentences)
    if total <= SHORT_TEXT_WORDS or len(sentences) < 3:
        return [" ".join(sentences)] if sentences else []
    groups: list[list[str]] = [[]]
    for sentence in sentences:
        current = groups[-1]
        size = sum(_words(s) for s in current)
        shift = bool(_TOPIC_SHIFT.match(sentence))
        # "And…"/"But…" may run a paragraph a little over; a sentence that
        # points back at the previous one may run it further
        if _POINTS_BACK.match(sentence) or _words(sentence) <= _SHORT_FOLLOW_UP_WORDS:
            limit = MAX_PARAGRAPH_WORDS * 1.5
        elif _CONTINUATION.match(sentence):
            limit = MAX_PARAGRAPH_WORDS * 1.25
        else:
            limit = MAX_PARAGRAPH_WORDS
        too_long = size >= limit
        if current and ((shift and size >= MIN_PARAGRAPH_WORDS) or too_long):
            groups.append([])
        groups[-1].append(sentence)
    # a short final paragraph reads as a dangling afterthought: fold it back
    if (len(groups) > 1 and sum(_words(s) for s in groups[-1]) < 12
            and not _TOPIC_SHIFT.match(groups[-1][0])):
        groups[-2].extend(groups.pop())
    return [" ".join(group) for group in groups]


def _intro(sentences: list[str]) -> int:
    """How many opening sentences are a greeting or a short lead-in
    ("Hey Terrence, how are you?", "Okay, let's start with this.") that
    reads better above the points than as a point of its own."""
    n = 0
    for sentence in sentences[:2]:
        words = _words(sentence)
        if (_GREETING.match(sentence) and words <= 12) or (
                words <= 8 and not sentence.endswith("?")):
            n += 1
        else:
            break
    return n


def _points(sentences: list[str]) -> list[str] | None:
    """Group sentences into bullet points, or None when the text does not
    break into at least two points."""
    head = _intro(sentences)
    intro, rest = sentences[:head], sentences[head:]
    groups: list[list[str]] = [[]]
    for sentence in rest:
        current = groups[-1]
        size = sum(_words(s) for s in current)
        follows = (_POINTS_BACK.match(sentence)
                   or _words(sentence) <= _SHORT_FOLLOW_UP_WORDS)
        # "You know?", "So that I can…", "Because…" can't start a point
        if current and (_words(sentence) <= _SHORT_FOLLOW_UP_WORDS
                        or _DEPENDENT.match(sentence)):
            current.append(sentence)
            continue
        if follows:
            limit = POINT_MAX_WORDS * 1.5
        elif _CONTINUATION.match(sentence):
            limit = POINT_MAX_WORDS * 1.25
        else:
            limit = POINT_MAX_WORDS
        # a question usually closes one ask; the next sentence starts another
        # (questions asked back to back are one ask)
        after_question = (current and current[-1].endswith("?")
                          and not sentence.endswith("?")
                          and not follows and not _CONTINUATION.match(sentence))
        new_topic = bool(_TOPIC_SHIFT.match(sentence)) or after_question
        if current and ((new_topic and size >= POINT_MIN_WORDS) or size >= limit):
            groups.append([])
        groups[-1].append(sentence)
    groups = [g for g in groups if g]
    # a tiny last point reads as an afterthought: keep it with the one before
    if len(groups) > 1 and sum(_words(s) for s in groups[-1]) < 6:
        groups[-2].extend(groups.pop())
    if len(groups) < 2:
        return None
    bullets = "\n".join(BULLET + " ".join(g) for g in groups)
    return [" ".join(intro) + "\n" + bullets] if intro else [bullets]


def _prose(sentences: list[str], style: str, chat: bool) -> list[str]:
    if style == "points" and sum(_words(s) for s in sentences) >= POINTS_MIN_WORDS:
        points = _points(sentences)
        if points is not None:
            return points
    if chat:  # a chat message stays one message, not paragraphs
        return [" ".join(sentences)] if sentences else []
    return _paragraphs(sentences)


def format_layout(text: str, style: str = "points", chat: bool = False) -> str:
    """Add bullet points (style "points") or paragraph breaks (style
    "paragraphs"), and numbered lists. Wording is unchanged.

    `chat` is for messaging apps: long dictations still get points, but
    shorter ones stay a single message instead of paragraphs."""
    if not text.strip():
        return text
    blocks: list[str] = []
    # respect any line breaks already present (e.g. from the cleanup model)
    for existing in re.split(r"\n\s*\n", text.strip()):
        if re.match(r"^\s*(?:\d+[.)]|[-*•])\s", existing):
            blocks.append(existing.strip())  # already a list
            continue
        sentences = split_sentences(" ".join(existing.split()))
        while sentences:
            found = _find_list(sentences)
            if found is None:
                blocks.extend(_prose(sentences, style, chat))
                break
            start, items, end = found
            blocks.extend(_prose(sentences[:start], style, chat))
            blocks.append("\n".join(f"{n}. {' '.join(item)}"
                                    for n, item in enumerate(items, 1)))
            sentences = sentences[end:]
    return "\n\n".join(block for block in blocks if block)
