"""Runtime response-language helper for the SuperOnes Chinese-first distribution.

Hard-coded user-facing runtime messages (approval gates, staffing prompts)
check `is_zh()` to pick Simplified Chinese. The distribution DEFAULTS to
Chinese: an unset AMPULY_RESPONSE_LANGUAGE means zh-CN; set it to e.g. "en"
to switch runtime messages back to English. LLM output language is handled
by the matching system-prompt directive in runtime_v2.
"""
from __future__ import annotations

import os


def response_language() -> str:
    return os.environ.get("AMPULY_RESPONSE_LANGUAGE", "").strip() or "zh-CN"


def is_zh() -> bool:
    return response_language().lower().startswith("zh")


# Chinese control-command aliases → canonical English tokens understood by the
# checkpoint/gate parsers. Exact whole-reply matches only, so ordinary feedback
# text is never rewritten.
_ZH_CONTROL_ALIASES = {
    "批准": "approve",
    "同意": "approve",
    "通过": "approve",
    "确认": "approve",
    "完全同意": "fully agree",
    "自动": "auto",
    "自动招聘": "auto recruit",
    "重新招聘": "recruit",
    "拒绝": "deny",
    "否决": "deny",
    "不同意": "deny",
    "停止": "stop",
    "取消": "cancel",
    "终止": "abort",
    "继续": "continue",
    "忽略": "ignore",
    "跳过": "skip",
    "知悉": "dismiss",
    "已知悉": "dismiss",
    "关闭": "dismiss",
    "反馈": "feedback",
}


def normalize_control_reply(normalized_reply: str) -> str:
    """Map a whole Chinese control command to its canonical English token.

    Input is expected to be already whitespace-normalized/lowercased; anything
    that is not an exact alias passes through unchanged.
    """
    return _ZH_CONTROL_ALIASES.get(str(normalized_reply or "").strip(), normalized_reply)
