"""Deterministic draft repairs that keep validator failures diagnosable.

Repairs run before validation and do not grant execution, close a case, or
accept an affirmative refund, closure, or delivery promise. Negated wording and
a quoted customer request are not those claims. Anything still invalid is
replaced by the safe fallback; the redacted candidate is kept for diagnosis.
"""
from copy import deepcopy
import json
import math
import re

from .understanding import normalize_understanding


_PROMISE_PATTERNS = (
    re.compile(r"(?:已经|已)(?:为您|为你|帮您|帮你)?(?:完成)?(?:退款|补发|赔付|转接|提交|取消|改好|关闭|解决|处理完|到账)"),
    re.compile(r"(?:退款|补发|赔付|工单|问题)(?P<gap>.{0,5})(?:完成|成功|关闭)"),
    re.compile(r"(?:保证|一定|肯定)(?P<gap>.{0,8})(?:到货|送达|退款|治好)"),
    re.compile(r"(?:难缠|情绪化|焦虑型)客户|你(?:太|很)?情绪化"),
    # Bare 已经完成 is a claim. The optional 完成 inside the first pattern is
    # only a prefix of 已经完成退款, so 是否关闭已经完成 would otherwise miss.
    re.compile(r"(?:已经|已)(?:为您|为你|帮您|帮你)?完成(?![\u4e00-\u9fff])"),
)
_INDETERMINATE = re.compile(r"是否|有没有|能否|会不会")
# Longer cues first so 不能 wins over the bare 不 inside it.
_GOVERNORS = ("不能", "不会", "不可", "无法", "没法", "并未", "尚未", "还未", "没有",
              "不是", "并不", "先不", "先别", "暂不", "不要", "别", "没", "未", "不", "勿")
# Polite or temporal words that contain 不/没/未/别 but do not negate the claim.
_IDIOMS = ("不要担心", "别担心", "别着急", "没问题", "不好意思", "不客气", "不用", "未来")
# Speech and epistemic verbs, plus 把/将 and a short object, may sit between
# the negation and the predicate. A comma or 但 is not part of this bridge.
_BRIDGE = re.compile(
    r"(?:把|将|给|向|对|跟|用|拿|以|靠|说|写成|说成|回复|称为|标成|标记为|标记成|改成|改为|称作|告诉你|告诉您|告诉|"
    r"宣称|声称|声明|断言|确认|当作|视为|认定|表示|承诺|保证|"
    r"这个问题|问题|这次|它|成|为|你|您|的|退款|工单|售后|投诉|换货|补发|赔付)*")
# 不能把未发生的事说成…: a negation governs a 把/将 frame even when the short
# object itself contains 未/没/不. Clause punctuation ends the frame.
_BA_FRAME = re.compile(
    r"(?:不能|不会|不可|无法|没法|不要|不该|不应|别|勿)(?P<ba>把|将)[^，,。；;！？!?\n]{1,12}?"
    r"(?:说成|说是|写成|标成|标记为|标记成|当作|当成|视为|称为|称作|改成|改为)\s*$")
# 无法保证 ST032 今晚一定送达: a negated guarantee verb governs a claim after a
# short product or time phrase. Punctuation or 但/不过/可是/而且 ends the frame.
_VERB_FRAME = re.compile(
    r"(?:不能|不会|不可|无法|没法|不敢)(?P<verb>保证|承诺|确保|确认|宣称|声称|答应)"
    r"(?:(?!但|不过|可是|而且|并且|然后)[^，,。；;！？!?\n]){0,12}$")
_GAP_BREAK = re.compile(r"[，,。；;！？!?\n]|但|不过|可是|而且|然后")
_COORDINATOR = re.compile(r"(?:或者|也不|或|和|与|、)\s*$")
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"))
_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|authorization|access[_-]?token|password|secret|credential|cookie)")


def _idiom_spans(text):
    spans = []
    for idiom in _IDIOMS:
        start = 0
        while True:
            found = text.find(idiom, start)
            if found < 0:
                break
            spans.append((found, found + len(idiom)))
            start = found + len(idiom)
    return spans


def _in_idiom(text, start, end):
    return any(left <= start and end <= right for left, right in _idiom_spans(text))


def _rightmost_governor(text):
    """End index of the rightmost real negation, or None.

    Bare 不/没/未/别/勿 inside 不用、别担心、未来 and the other listed idioms
    do not govern the claim.
    """
    best = None
    for cue in _GOVERNORS:
        start = 0
        while True:
            found = text.find(cue, start)
            if found < 0:
                break
            end = found + len(cue)
            if not _in_idiom(text, found, end) and (
                    best is None or end > best[0] or (end == best[0] and end - found > best[1])):
                best = (end, end - found)
            start = found + 1
    return None if best is None else best[0]


def _span_has_governing_negation(span):
    return _rightmost_governor(span) is not None


def _sentence_tail(before):
    cut = max(before.rfind(mark) for mark in "。！？\n")
    return before[cut + 1:]


def _prefix_governs(prefix):
    """True when a real negation sits on the claim, not merely earlier in the clause.

    说/写成/标成/告诉你 may stand between the negation and the predicate.
    别担心、不用着急、没问题、不好意思、不客气 and 未来 do not negate it.
    """
    # Keep clause punctuation. Stripping ， would let 无法宣称 govern 工单已关闭.
    tail = _sentence_tail(prefix).rstrip(" \t　\"“”‘’「」『』")
    framed = _BA_FRAME.search(tail)
    if framed is not None and not _in_idiom(tail, framed.start(), framed.start("ba")):
        return True
    framed = _VERB_FRAME.search(tail)
    if framed is not None and not _in_idiom(tail, framed.start(), framed.start("verb")):
        return True
    end = _rightmost_governor(tail)
    if end is None:
        return False
    return _BRIDGE.fullmatch(tail[end:]) is not None


def _inside_quotes(text, start, end):
    for open_mark, close_mark in _QUOTE_PAIRS:
        search = 0
        while search < start:
            left = text.find(open_mark, search)
            if left < 0 or left >= start:
                break
            right = text.find(close_mark, left + len(open_mark))
            if right < 0:
                break
            if left < start and end <= right:
                return True
            search = right + len(close_mark)
    return False


def _quoted_customer_span(before, span, customer_text):
    if not customer_text or span not in customer_text:
        return False
    found = re.search(
        r"(写成|说成|原话|你说|你希望|你要求|回复成|标成|称作|称为|写上|告诉你|告诉您)(?P<mid>.{0,40})$",
        before)
    if found is None or re.search(r"保证|一定|肯定|已经|已为|已帮", found.group("mid")):
        return False
    return True


def _coordinated_with_negated_claim(reply, start, customer_text):
    """True when 或/和/与/、/也不 joins this claim to an earlier negated one.

    但 and a comma do not carry the negation forward.
    """
    tail = _sentence_tail(reply[:start])
    coordinator = _COORDINATOR.search(tail)
    if coordinator is None:
        return False
    head = tail[:coordinator.start()].rstrip()
    if not head:
        return False
    head_end = start - len(tail) + len(head)
    window = reply[:head_end]
    for pattern in _PROMISE_PATTERNS:
        for match in pattern.finditer(window):
            if match.end() == head_end and promise_span_allowed(
                    reply, match.start(), match.end(), customer_text):
                return True
    return False


def _indeterminate_claim(reply, start, end, gap):
    """是否 / 与否 / 有没有 / 能否 / 会不会 question the predicate; they are not claims.

    A following 是否 does not cancel an earlier assertion (已关闭，是否还有问题).
    """
    if type(gap) is str and _INDETERMINATE.search(gap):
        return True
    return reply[end:end + 2] == "与否"


def promise_span_allowed(reply, start, end, customer_text):
    """True only when this match is governed by a negation or is the customer's quoted request."""
    if type(reply) is not str or not 0 <= start <= end <= len(reply):
        return False
    span = reply[start:end]
    gap = None
    for pattern in _PROMISE_PATTERNS:
        matched = pattern.fullmatch(span)
        if matched is not None:
            gap = matched.groupdict().get("gap")
            break
    if _indeterminate_claim(reply, start, end, gap):
        return True
    if type(gap) is str and _GAP_BREAK.search(gap):
        # 无法保证质量但今晚一定送达: the negation does not reach past a clause break.
        return False
    if _span_has_governing_negation(span):
        return True
    if _prefix_governs(reply[:start]):
        return True
    customer_text = customer_text or ""
    if _coordinated_with_negated_claim(reply, start, customer_text):
        return True
    before = _sentence_tail(reply[:start])
    if span in customer_text and (_inside_quotes(reply, start, end) or _quoted_customer_span(before, span, customer_text)):
        return True
    return False


def promise_violation(reply, customer_text=""):
    """Affirmative unsupported execution or a customer label. Governed refusals pass."""
    if type(reply) is not str or not reply:
        return False
    customer_text = customer_text if type(customer_text) is str else ""
    spans = {(match.start(), match.end())
             for pattern in _PROMISE_PATTERNS for match in pattern.finditer(reply)}
    allowed = {span for span in spans if promise_span_allowed(reply, span[0], span[1], customer_text)}
    for start, end in spans - allowed:
        # A shorter match nested inside an allowed claim (不能把工单标记为已关闭 also
        # matches 已关闭) shares that claim's negation or quotation.
        if not any(a_start <= start and end <= a_end and (a_start, a_end) != (start, end)
                   for a_start, a_end in allowed):
            return True
    return False


def _shrink_keeping_governors(sentence, maximum):
    """Shorten one sentence without cutting a negation off its claim."""
    if len(sentence) <= maximum:
        return sentence
    prefix = sentence[:maximum]
    suffix = sentence[-maximum:]
    if not promise_violation(prefix) or promise_violation(sentence):
        return prefix
    if not promise_violation(suffix) or promise_violation(sentence):
        return suffix
    return ""


def _truncate_reply(text, maximum):
    """Fit a reply. Truncation must not turn a refusal into an affirmative claim.

    A safe reply stays safe. An affirmative claim is not edited into harmless
    filler; the checker after repair still sees it.
    """
    if type(text) is not str or maximum < 1 or len(text) <= maximum:
        return text
    sentences = [part for part in re.split(r"(?<=[。！？\n])", text) if part]
    if promise_violation(text):
        for part in sentences:
            if not promise_violation(part):
                continue
            piece = part if len(part) <= maximum else part[:maximum]
            if promise_violation(piece):
                return piece
        return text[:maximum]
    kept = ""
    for part in reversed(sentences):
        piece = _shrink_keeping_governors(part, maximum) if len(part) > maximum else part
        if not piece or len(piece) + len(kept) > maximum:
            continue
        candidate = piece + kept
        if promise_violation(candidate):
            continue
        kept = candidate
    if kept:
        return kept
    head = text[:maximum]
    return "" if promise_violation(head) else head


def _repair_text(value, schema, path):
    if "enum" in schema:
        stripped = value.strip()
        return stripped if stripped in schema["enum"] else value
    cleaned = "".join(char for char in value if ord(char) >= 32 or char in "\n\t").strip()
    maximum = schema.get("maxLength", 400)
    if type(maximum) is not int or maximum < 1:
        maximum = 400
    if not cleaned:
        if path[-1:] == ("response_strategy",):
            return "承认具体诉求，不猜测情绪，不承诺执行结果。"[:maximum]
        if path[-1:] == ("suggested_team",):
            return "客服"[:maximum]
        return cleaned
    if len(cleaned) > maximum:
        cleaned = _truncate_reply(cleaned, maximum) if path[-1:] == ("reply",) else cleaned[:maximum].rstrip()
    return cleaned


def _repair_strings(value, schema, path):
    if type(schema) is not dict:
        return value
    kind = schema.get("type")
    if kind == "object" and type(value) is dict:
        properties = schema.get("properties")
        if type(properties) is dict:
            for key, child in properties.items():
                if key in value:
                    value[key] = _repair_strings(value[key], child, path + (key,))
        return value
    if kind == "array" and type(value) is list:
        item = schema.get("items", {})
        return [_repair_strings(entry, item, path) for entry in value]
    if kind == "string" and type(value) is str:
        return _repair_text(value, schema, path)
    return value


def repair_model_candidate(candidate, turns, schema):
    """Return a new candidate. The caller's object is not modified."""
    if type(candidate) is not dict:
        return candidate
    candidate = deepcopy(candidate)
    if type(turns) is list and type(candidate.get("understanding")) is dict:
        candidate["understanding"] = normalize_understanding(candidate["understanding"], turns)
    if type(schema) is dict:
        _repair_strings(candidate, schema, ())
    reply = candidate.get("reply")
    summary = candidate.get("summary")
    if type(reply) is str and not reply.strip() and type(summary) is str and summary.strip():
        candidate["reply"] = _truncate_reply(summary.strip(), 1200)
    return candidate


def _sanitize(value, depth=0):
    if depth > 8:
        return "…"
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return value if abs(value) < 10**12 else 0
    if type(value) is float:
        return value if math.isfinite(value) and abs(value) < 10**12 else None
    if type(value) is str:
        return "".join(char for char in value if ord(char) >= 32 or char in "\n\t")[:1500]
    if type(value) is list:
        return [_sanitize(item, depth + 1) for item in value[:32]]
    if type(value) is dict:
        cleaned = {}
        for key, item in list(value.items())[:40]:
            if type(key) is not str:
                continue
            name = key[:80]
            cleaned[name] = "[redacted]" if _SECRET_KEY.search(name) else _sanitize(item, depth + 1)
        return cleaned
    return None


def redact_rejected_candidate(candidate, code):
    """Bounded JSON for a rejected model candidate. Secrets are not copied."""
    if type(code) is not str or not code.strip():
        code = "invalid_response"
    try:
        text = json.dumps({"validation_error": code[:128], "candidate": _sanitize(candidate)},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    if len(text) > 4000:
        text = text[:3988] + "…[truncated]"
    return text or None
