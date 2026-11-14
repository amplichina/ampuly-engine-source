#!/usr/bin/env python3
# 来源：ZJU-REAL/Easel@3f1d4fb8b99694f7231ee97163b637df89b50d3b（Apache-2.0）
#       skills/shared/scripts/platform_readback.py
# 移植：安普里 2026-10-22，落点 ampuly.layer4_tools.selfmedia.readback。
# 改动：只保留与浏览器无关的判定层（作品模型、标题/时间窗/发前快照匹配、多轮读回
#       状态机、状态归一化）；原文件的 Playwright 抓取函数（read_* / capture_* /
#       verify_* 的 page 参数）未移植——抓取通道属 Computer Use，由宿主另行接线。
#       登记见 THIRD_PARTY_NOTICES.md。
"""readback.py — 发布读回对账的纯判定层。

发布成功与否**只由平台侧证据判定**，不由脚本出口或页面提示判定。判定结果为四态：

``verified``        读回对上（标题前缀 + 时间窗 + 发前快照排除）
``unverified``      读回通道通，但多轮未见新作品（索引延迟/审核队列），保留待查
``login_required``  读回时登录态已失效——**结果未知**，不是失败
``readback_error``  读回通道本身失败（网络/页面改版），**结果未知**

铁律：``unverified`` / ``login_required`` / ``readback_error`` 都**不允许自动重发**。
``ReadbackResult.may_republish`` 恒为 False，重发只能由人确认。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

TITLE_KEY_LEN = 12
"""标题前缀匹配长度：与上游 douyin_publish 的对账口径一致。"""

SINCE_TOLERANCE_MS = 10 * 60 * 1000
"""时间窗容差：吸收平台时间戳精度与索引延迟。"""

OUTCOMES = ("verified", "unverified", "login_required", "readback_error")


@dataclass
class WorkItem:
    platform_content_id: str
    title: str
    status: str
    published_at_ms: int | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform_content_id": self.platform_content_id,
            "title": self.title,
            "status": self.status,
            "published_at_ms": self.published_at_ms,
            "stats": self.stats,
        }


@dataclass
class ReadbackResult:
    outcome: str
    matched: WorkItem | None = None
    candidates: list[WorkItem] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown readback outcome: {self.outcome!r}")
        if self.outcome == "verified" and self.matched is None:
            raise ValueError("verified requires a matched WorkItem")

    @property
    def requires_manual_check(self) -> bool:
        """非 verified 一律要人核对；不能把未知写成失败，也不能自动重发。"""
        return self.outcome != "verified"

    @property
    def may_republish(self) -> bool:
        """任何读回结论都不授权自动重发。"""
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "matched": self.matched.as_dict() if self.matched else None,
            "candidates": [w.as_dict() for w in self.candidates],
            "evidence": self.evidence,
            "error": self.error,
        }


class LoginRequiredError(RuntimeError):
    """读回时确认登录态已失效（区别于通道故障——那是 readback_error）。"""


def normalize_douyin_status(status: Any, public_time: Any = None,
                            *, now: float | None = None) -> str:
    """抖音作品状态归一化（平台状态码 → 统一枚举）。"""
    if isinstance(status, (int, float)):
        return str(status)
    s = status if isinstance(status, dict) else {}
    if s.get("is_delete"):
        return "deleted"
    if s.get("is_prohibited"):
        return "prohibited"
    if s.get("in_reviewing"):
        return "reviewing"
    if s.get("is_private"):
        return "private"
    try:
        public_s = float(public_time or 0)
    except (TypeError, ValueError):
        public_s = 0
    if public_s > (time.time() if now is None else now):
        return "scheduled"
    return "published"


_BILIBILI_STATE = {
    0: "published", -1: "pending", -2: "pending", -30: "pending",
}


def normalize_bilibili_state(code: Any) -> str:
    """B 站稿件 state 归一化；未知码保留原文，不猜成成功。"""
    try:
        return _BILIBILI_STATE.get(int(code), f"state:{code}")
    except (TypeError, ValueError):
        return "unknown"


def first_id(item: dict[str, Any], keys: Sequence[str] = ()) -> str:
    """按优先级取平台内容 ID；取不到返回空串（调用方不得伪造 ID）。"""
    for key in tuple(keys) + ("aweme_id", "item_id", "id"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def find_published_work(works: Sequence[WorkItem], title: str, *,
                        since_ms: int | None = None,
                        key_len: int = TITLE_KEY_LEN,
                        exclude_ids: set[str] | None = None) -> WorkItem | None:
    """对账：在作品列表里找属于本次发布的那一条。

    - 标题为空 → 无法对账（返回 None，调用方按 unverified 处理）。
    - ``since_ms`` 给了时：发布时间明显早于它的跳过；容差 ``SINCE_TOLERANCE_MS``
      吸收平台时间戳精度与索引延迟。
    - ``exclude_ids``（发前快照）中的 id 一定是旧作品，比时间窗更硬。
    """
    key = (title or "").strip().replace("\n", " ")[:key_len]
    if not key:
        return None
    for w in works:
        if key not in (w.title or ""):
            continue
        if exclude_ids and w.platform_content_id in exclude_ids:
            continue
        if since_ms and w.published_at_ms and (w.published_at_ms + SINCE_TOLERANCE_MS) < since_ms:
            continue
        return w
    return None


def verify_publish(
    reader: Callable[[], Sequence[WorkItem]],
    *,
    title: str,
    since_ms: int | None = None,
    exclude_ids: set[str] | None = None,
    attempts: int = 3,
    delay_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    account_reader: Callable[[], dict[str, Any]] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ReadbackResult:
    """多轮读回对账。除 KeyboardInterrupt 外不抛异常。

    ``reader`` 是抓取通道的适配器（Computer Use 侧实现）；本函数只做判定。
    """
    last_error: Exception | None = None
    last_candidates: list[WorkItem] = []
    evidence: dict[str, Any] = {"attempts": 0}

    def _say(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    for i in range(max(1, attempts)):
        evidence = {"attempts": i + 1, "checked_at": int(time.time())}
        try:
            works = list(reader())
        except LoginRequiredError as exc:
            return ReadbackResult(outcome="login_required", evidence=evidence, error=str(exc))
        except Exception as exc:  # noqa: BLE001 —— 通道故障不能升级成失败或重发
            last_error = exc
            _say(f"readback round {i + 1}/{attempts} failed: {exc}")
            if i + 1 < attempts and delay_s:
                sleep(delay_s)
            continue

        last_candidates = works
        evidence["count"] = len(works)
        matched = find_published_work(works, title, since_ms=since_ms,
                                      exclude_ids=exclude_ids)
        if matched:
            if account_reader is not None:
                try:
                    evidence["account"] = account_reader()
                except Exception as exc:  # noqa: BLE001 —— 账号快照只是回执标准件
                    evidence["account_error"] = str(exc)
            return ReadbackResult(outcome="verified", matched=matched,
                                  candidates=works[:5], evidence=evidence)
        _say(f"readback round {i + 1}/{attempts}: {len(works)} items, no match yet")
        if i + 1 < attempts and delay_s:
            sleep(delay_s)

    if last_error is not None and not last_candidates:
        return ReadbackResult(outcome="readback_error", evidence=evidence, error=str(last_error))
    return ReadbackResult(outcome="unverified", candidates=last_candidates[:5], evidence=evidence)
