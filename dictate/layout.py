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


# ------------------------------------------------------- spoken lists --
# "The list of tasks I have are hitting the gym, and then working on three
# assignments, and also studying for a midterm." -> an intro line and one
# bullet per item. The words that only joined the items ("and then",
# "also") are dropped, like the "first… second…" markers above.
_LIST_NOUNS = (r"(?:list|things|tasks|items|to-?dos|priorities|steps|goals|errands|"
               r"questions|points|requirements|options|ideas|topics|features|"
               r"changes|chores)")
_LIST_INTRO = re.compile(
    rf"^(?P<intro>.*?\b(?:"
    rf"{_LIST_NOUNS}\b[^.?!:]{{0,45}}?\b(?:are|is|include|includes|would be|will be)"
    rf"(?:\s+(?:going to be|gonna be|like))?"
    rf"|(?:i|we) (?:need|have|want|got|gotta) to (?:do|get done|finish|handle|cover|buy|get)"
    rf"(?: (?:today|tonight|tomorrow|this week|this weekend))?(?: (?:is|are))?"
    rf"|here(?:'s| is| are) (?:my|the|what|a)\b[^.?!:]{{0,35}}?"
    rf"|the following(?: \w+)?(?: (?:is|are))?"
    rf"))\s*[:,]?\s+(?P<items>\S.*)$",
    re.IGNORECASE)
_COLON_INTRO = re.compile(r"^(?P<intro>[^:]{3,120}):\s+(?P<items>\S.*)$")
# Joining words between items, and a following sentence that adds an item.
_ITEM_SPLIT = re.compile(
    r"\s*(?:,|;)\s*(?:and\s+)?(?:then\s+)?(?:also\s+)?(?:after that\s+)?"
    r"|\s+and then\s+(?:also\s+)?|\s+and also\s+", re.IGNORECASE)
_ITEM_LEAD = re.compile(r"^(?:(?:and|then|also|plus|lastly|finally|after that)\b,?\s*)+",
                        re.IGNORECASE)
_KEEP_DOT = ("a.m.", "p.m.", "etc.", "e.g.", "i.e.", "vs.", "inc.", "jr.", "sr.")
_ADDS_ITEM = re.compile(r"^(?:and\s+)?(?:also|then|plus|lastly|finally|and then)\b", re.IGNORECASE)
# A piece starting like this belongs to the item before it ("the report,
# which is due Friday").
_ITEM_TAIL = re.compile(r"^(?:which|who|that|because|so|but|or|since|while|where|when|"
                        r"either|it's|it is|that's|actually|no|sorry|i mean|"
                        r"and\s+(?:i|we|it|that|this)\b)\b", re.IGNORECASE)
MAX_ITEM_WORDS = 22


def _items(text: str) -> list[str]:
    pieces = [p.strip() for p in _ITEM_SPLIT.split(text) if p and p.strip()]
    items: list[str] = []
    for piece in pieces:
        if items and _ITEM_TAIL.match(piece):
            items[-1] = f"{items[-1]}, {piece}"
            continue
        piece = _ITEM_LEAD.sub("", piece).strip().rstrip("!")
        if piece.endswith(".") and not piece.lower().endswith(_KEEP_DOT):
            piece = piece[:-1]
        if piece:
            items.append(piece[0].upper() + piece[1:])
    return items


def _find_spoken_list(sentences: list[str]) -> tuple[int, str, list[str], int] | None:
    """(sentence index, intro text, items, index after the list) or None."""
    for i, sentence in enumerate(sentences):
        match = _LIST_INTRO.match(sentence) or _COLON_INTRO.match(sentence)
        if not match:
            continue
        text = match.group("items")
        end = i + 1
        # "Also, I have a meeting at 4." adds one more item
        while end < len(sentences) and _ADDS_ITEM.match(sentences[end]) \
                and _words(sentences[end]) <= MAX_ITEM_WORDS:
            text = f"{text.rstrip('!')}, {_ITEM_LEAD.sub('', sentences[end])}"
            end += 1
        intro_text = match.group("intro")
        if intro_text.count('"') % 2 or intro_text.count("“") > intro_text.count("”"):
            continue  # inside a quotation: someone else's words, not a list
        items = _items(text)
        named = bool(re.search(rf"\b{_LIST_NOUNS}\b", intro_text, re.IGNORECASE))
        short = all(_words(item) <= 6 for item in items)
        if match.re is _COLON_INTRO and not named and not (len(items) >= 3 and short):
            continue  # "it should be like: …" is prose, not a list
        one_word = any(_words(item) == 1 for item in items)
        if one_word and not all(_words(item) <= 3 for item in items):
            continue  # "Honestly" is a filler that got split off, not an item
        if (len(items) >= 3 or (len(items) == 2 and named and match.re is _COLON_INTRO)) \
                and all(_words(item) <= MAX_ITEM_WORDS for item in items):
            intro = match.group("intro").strip().rstrip(",")
            return i, intro, items, end
    return None


def _paragraphs(sentences: list[str], short_text: int = SHORT_TEXT_WORDS,
                max_words: int = MAX_PARAGRAPH_WORDS) -> list[str]:
    total = sum(_words(s) for s in sentences)
    if total <= short_text or len(sentences) < 3:
        return [" ".join(sentences)] if sentences else []
    groups: list[list[str]] = [[]]
    for sentence in sentences:
        current = groups[-1]
        size = sum(_words(s) for s in current)
        shift = bool(_TOPIC_SHIFT.match(sentence))
        # "And…"/"But…" may run a paragraph a little over; a sentence that
        # points back at the previous one may run it further
        if _POINTS_BACK.match(sentence) or _words(sentence) <= _SHORT_FOLLOW_UP_WORDS:
            limit = max_words * 1.5
        elif _CONTINUATION.match(sentence):
            limit = max_words * 1.25
        else:
            limit = max_words
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


# ---------------------------------------------------------------- email --
# "Hi Derek, hope you're well. … Thank you, Rohan." becomes
#
#   Hi Derek,
#
#   Hope you're well. …
#
#   Thank you,
#   Rohan
_EMAIL_GREETING = re.compile(
    r"^(?P<greeting>(?:hi|hey|hello|dear|greetings|good (?:morning|afternoon|evening))"
    r"(?:\s+[^\s,.!?:;]+){0,3})\s*[,.!:]\s+(?P<rest>\S.*)$",
    re.IGNORECASE | re.DOTALL)
_SIGN_OFF_WORDS = (
    r"(?:thanks|thank you)(?: (?:so|very) much)?(?: again)?|many thanks|"
    r"best(?: regards| wishes)?|kind regards|warm regards|warmly|regards|cheers|"
    r"sincerely|talk soon|all the best|take care|looking forward to it")
# The last sentence: a sign-off, optionally followed by a capitalised name.
_EMAIL_SIGN_OFF = re.compile(
    rf"(?:^|(?<=[.!?])\s+)(?P<sign>(?i:{_SIGN_OFF_WORDS}))[,.!]?"
    r"(?:\s+(?P<name>[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)?))?[.!]?\s*$")
_EMAIL_PLEASANTRY = re.compile(
    r"\bhope (?:you(?:'re| are)(?: doing)? (?:well|good|great)|all is well|"
    r"this (?:email|message|note) finds you|you(?:'re| are) having a (?:good|great))",
    re.IGNORECASE)
# A closing line like "Let me know if…" reads best as its own paragraph.
_EMAIL_CLOSER = re.compile(
    r"^(?:(?:and|so|also)[,]?\s+)?(?:please\s+)?(?:let me know|looking forward|"
    r"feel free|hope to hear|i look forward|talk soon|happy to)\b", re.IGNORECASE)


def looks_like_email(text: str) -> bool:
    """A greeting with a sign-off or an "I hope you're well": an email,
    whichever app it is being written in."""
    flat = " ".join(text.split())
    if not _EMAIL_GREETING.match(flat):
        return False
    return bool(_EMAIL_SIGN_OFF.search(flat) or _EMAIL_PLEASANTRY.search(flat))


def format_email(text: str) -> str | None:
    """Greeting, body paragraphs and sign-off each on their own lines.
    None when the text has neither a greeting nor a sign-off."""
    flat = " ".join(text.split())
    greeting = None
    rest = flat
    match = _EMAIL_GREETING.match(flat)
    if match:
        greeting = match.group("greeting").strip() + ","
        rest = match.group("rest")
        rest = rest[0].upper() + rest[1:]
    closing = None
    sign = _EMAIL_SIGN_OFF.search(rest)
    if sign and sign.start() > 0:
        word = sign.group("sign")
        word = word[0].upper() + word[1:]
        name = sign.group("name")
        closing = f"{word},\n{name}" if name else rest[sign.start():].strip()
        rest = rest[:sign.start()].strip()
    if greeting is None and closing is None:
        return None
    sentences = split_sentences(rest)
    closer = []
    if len(sentences) >= 2 and _EMAIL_CLOSER.match(sentences[-1]):
        closer = [sentences.pop()]
    body = _paragraphs(sentences, short_text=30, max_words=35) if sentences else []
    blocks = [greeting, *body, " ".join(closer), closing]
    return "\n\n".join(b for b in blocks if b)


def format_layout(text: str, style: str = "points", chat: bool = False,
                  email: bool = False) -> str:
    """Add bullet points (style "points") or paragraph breaks (style
    "paragraphs"), and numbered lists. Wording is unchanged.

    `chat` is for messaging apps: long dictations still get points, but
    shorter ones stay a single message instead of paragraphs."""
    if not text.strip():
        return text
    # Emails: in an email app, or anything shaped like one (greeting +
    # sign-off), except chat messages, which stay one message.
    if (email or looks_like_email(text)) and not chat \
            and not re.search(r"(?m)^\s*(?:\d+[.)]|[-*•])\s", text):
        laid_out = format_email(text)
        if laid_out is not None:
            return laid_out
    blocks: list[str] = []
    # respect any line breaks already present (e.g. from the cleanup model)
    for existing in re.split(r"\n\s*\n", text.strip()):
        if re.match(r"^\s*(?:\d+[.)]|[-*•])\s", existing):
            blocks.append(existing.strip())  # already a list
            continue
        sentences = split_sentences(" ".join(existing.split()))
        while sentences:
            found = _find_list(sentences)
            spoken = _find_spoken_list(sentences)
            if spoken is not None and (found is None or spoken[0] < found[0]):
                start, intro, items, end = spoken
                lead = sentences[:start]
                head = " ".join(lead + [intro + ":"]) if intro else " ".join(lead)
                bullets = "\n".join(BULLET + item for item in items)
                blocks.append(f"{head}\n{bullets}" if head else bullets)
                sentences = sentences[end:]
                continue
            if found is None:
                blocks.extend(_prose(sentences, style, chat))
                break
            start, items, end = found
            blocks.extend(_prose(sentences[:start], style, chat))
            blocks.append("\n".join(f"{n}. {' '.join(item)}"
                                    for n, item in enumerate(items, 1)))
            sentences = sentences[end:]
    return "\n\n".join(block for block in blocks if block)
