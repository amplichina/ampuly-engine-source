"""平台规则与数据口径（第二轮吸纳 MatrixMedia 的适配器与统计设计）。

只吸收事实与协议，不复制其 GPL 源码。平台规则集中在这里，发布校验与复盘口径共用，
避免标题上限、声明选项、指标名散落在各 SKILL 与脚本里。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformRule:
    key: str
    name: str
    title_max: int
    body_required: bool
    tags_in_body: bool = False
    short_title_max: int = 0
    disclosures: tuple[str, ...] = ("ai_generated", "fiction", "none")
    advisory: bool = True  # 平台真实上限在页面上，静态表只作发布前预警


PLATFORMS: dict[str, PlatformRule] = {
    "xiaohongshu": PlatformRule("xiaohongshu", "小红书", 20, False, tags_in_body=False),
    "douyin": PlatformRule("douyin", "抖音", 55, False, tags_in_body=True),
    "kuaishou": PlatformRule("kuaishou", "快手", 55, False, tags_in_body=True),
    "channels": PlatformRule("channels", "视频号", 16, False, tags_in_body=True, short_title_max=16),
    "bilibili": PlatformRule("bilibili", "B站", 80, True, tags_in_body=False),
    "toutiao": PlatformRule("toutiao", "头条", 30, False, tags_in_body=False),
    "baijiahao": PlatformRule("baijiahao", "百家号", 30, False, tags_in_body=False),
    "zhihu": PlatformRule("zhihu", "知乎", 100, True, tags_in_body=False),
}

# 统一指标口径：日报/快照一律这六个，平台缺项记 null，不拿别的数字顶替。
METRICS = ("fans", "plays", "likes", "comments", "favorites", "shares")

# 采集阶梯：页面 fetch 默认 → 被动嗅探兜底 → 主进程直取仅限无签名开放平台。
COLLECTION_TIERS = ("page_fetch", "passive_sniff", "main_process")


def rule_for(platform: str) -> PlatformRule:
    try:
        return PLATFORMS[platform]
    except KeyError as exc:
        raise ValueError(f"unsupported platform: {platform}") from exc


def disclosure_options(platform: str) -> tuple[str, ...]:
    return rule_for(platform).disclosures


def metrics_snapshot(**values: int | None) -> dict[str, int | None]:
    unknown = set(values) - set(METRICS)
    if unknown:
        raise ValueError(f"unknown metrics: {sorted(unknown)}")
    return {key: values.get(key) for key in METRICS}

def normalize_account_ref(raw: str) -> str:
    """把历史形态规整为 <platform>:<account_key>。

    支持：platform:key、persist:<phone><platform>、persist:<phone>-N<platform>、<phone><platform>。
    认不出平台就拒绝，不猜。
    """
    value = str(raw or "").strip()
    if ":" in value and not value.startswith("persist:"):
        platform, key = value.split(":", 1)
        if platform in PLATFORMS and key:
            return f"{platform}:{key}"
    text = value[len("persist:"):] if value.startswith("persist:") else value
    for platform, rule in PLATFORMS.items():
        suffix = rule.name
        if text.endswith(suffix) and len(text) > len(suffix):
            key = text[: -len(suffix)].rstrip("-")
            if key:
                return f"{platform}:{key}"
    raise ValueError(f"unrecognized account ref: {raw!r}")

def with_observed(platform: str, *, title_max: int | None = None,
                  body_required: bool | None = None) -> "PlatformRule":
    """用适配器在页面上读到的真实上限覆盖静态表（MatrixMedia 的 maxlength 是运行时读的）。"""
    from dataclasses import replace
    rule = rule_for(platform)
    return replace(rule,
                   title_max=rule.title_max if title_max is None else int(title_max),
                   body_required=rule.body_required if body_required is None else bool(body_required),
                   advisory=False)
