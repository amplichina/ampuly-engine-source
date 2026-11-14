"""Deterministic commerce demo; this does not verify an LLM's understanding.

Fixtures are fictional, supplied by the caller, and copied on construction.
Catalog example::

    {"tenant_id": "tenant-demo", "shop_id": "shop-demo", "sku": "ST032",
     "name": "折叠收纳箱", "aliases": ["收纳箱"],
     "facts": {"材质": "PP 塑料", "容量": "32L", "价格": "59.00 元"},
     "source_id": "KB-ST032-v1", "source_title": "ST032 商品说明"}

Order example::

    {"tenant_id": "tenant-demo", "shop_id": "shop-demo", "buyer_id": "buyer-demo",
     "order_id": "DEMO-1001", "status": "已发货",
     "logistics": {"carrier": "虚构快递", "tracking_no": "TEST-1001",
                   "last_event": "演示包裹已到达分拨中心",
                   "estimated_delivery": "演示预计明日送达"}}

``reply`` context must come from a trusted channel adapter/server, rather than
buyer text or untrusted request fields. Product knowledge needs tenant/shop
scope. Orders additionally need a buyer ID and ``verified_customer is True``;
that flag alone does not bypass the three-part ownership check. Missing/False
verification is never treated as authenticated. No order-write API exists.
"""

from __future__ import annotations

from copy import deepcopy
import re


_ORDER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+|\d{6,24})(?![A-Za-z0-9_-])"
)
_NAMED_ORDER = re.compile(r"订单(?:号|编号)\s*[：:]?\s*([A-Za-z0-9][A-Za-z0-9_-]{1,63})")
_FACT_HINTS = {
    "材质": ("材质", "材料", "什么做"),
    "尺寸": ("尺寸", "大小", "长宽高"),
    "容量": ("容量", "多少升", "几升"),
    "价格": ("价格", "多少钱", "售价", "多少元"),
    "库存": ("库存", "有货", "现货"),
    "颜色": ("颜色", "色款"),
    "使用": ("怎么用", "使用", "安装"),
    "保养": ("保养", "清洗"),
    "退换货": ("退换", "退货政策", "退货规则", "退款政策", "售后规则", "能退", "可退", "能换", "可换"),
}
_POLICY_HINTS = ("退货政策", "退货规则", "退换货规则", "退款政策", "售后规则", "能退", "可退", "能换", "可换")
_HUMAN_ACTIONS = (
    ("change_address", "修改收货地址", ("改地址", "修改地址", "更改地址", "地址写错", "换地址")),
    ("cancel_order", "取消订单", ("取消订单", "撤销订单", "不要这单")),
    ("refund", "申请退款", ("退款", "退钱")),
    ("return", "申请退货", ("退货",)),
    ("exchange", "申请换货", ("换货",)),
    ("after_sales", "申请人工售后", ("售后", "投诉", "破损", "坏了", "少件", "漏发")),
)


def _identity(value: object) -> str | None:
    if isinstance(value, str) and value and value == value.strip() and len(value) <= 128:
        return value
    return None


def _response(text: str, intent: str, sources: list[dict] | None = None,
              requires_human: bool = False) -> dict:
    return {
        "text": text,
        "intent": intent,
        "sources": sources if sources is not None else [],
        "requires_human": requires_human,
        "mode": "deterministic-demo",
    }


class BusinessAssistant:
    """Read scoped facts and owned orders, or produce a human-only draft.

    Matching is intentionally a small keyword/identifier rule set, not semantic
    model inference. Supply a SKU/name for product questions. A lone owned order
    can answer a general logistics question; multiple owned orders need an ID.
    The same order ID can appear in different shops without granting access.
    """

    def __init__(self, catalog: list[dict], orders: list[dict]) -> None:
        if not isinstance(catalog, list) or not isinstance(orders, list):
            raise ValueError("catalog and orders must be lists")
        self._catalog = deepcopy(catalog)
        self._orders = deepcopy(orders)
        product_keys: set[tuple[str, str, str]] = set()
        order_keys: set[tuple[str, str, str]] = set()
        for item in self._catalog:
            self._validate_fields(item, ("tenant_id", "shop_id", "sku", "name", "source_id", "source_title"))
            facts = item.get("facts")
            if not isinstance(facts, dict) or not facts or any(
                not isinstance(k, str) or not k or not isinstance(v, str) or not v
                for k, v in facts.items()
            ):
                raise ValueError("catalog facts must be a nonempty string mapping")
            aliases = item.get("aliases", [])
            if not isinstance(aliases, list) or any(not _identity(a) for a in aliases):
                raise ValueError("catalog aliases must be nonempty strings")
            key = (item["tenant_id"], item["shop_id"], item["sku"].casefold())
            if key in product_keys:
                raise ValueError("duplicate scoped product")
            product_keys.add(key)
        for item in self._orders:
            self._validate_fields(item, ("tenant_id", "shop_id", "buyer_id", "order_id", "status"))
            logistics = item.get("logistics", {})
            if not isinstance(logistics, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in logistics.items()
            ):
                raise ValueError("order logistics must be a string mapping")
            key = (item["tenant_id"], item["shop_id"], item["order_id"].casefold())
            if key in order_keys:
                raise ValueError("duplicate scoped order")
            order_keys.add(key)

    @staticmethod
    def _validate_fields(item: object, fields: tuple[str, ...]) -> None:
        if not isinstance(item, dict) or any(not _identity(item.get(field)) for field in fields):
            raise ValueError("fixture is missing a valid required field")

    def reply(self, context: dict, text: str) -> dict:
        """Return text, intent, citations, human flag and deterministic-demo mode.

        Context is trusted input; verification must be literal ``True``. Do not
        copy a claimed buyer identity or verification flag out of customer text.
        """
        if not isinstance(context, dict) or not _identity(context.get("tenant_id")) or not _identity(context.get("shop_id")):
            return _response("无法确认当前店铺范围，请由可信渠道确认店铺后重试。", "identity_required", requires_human=True)
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return _response("请简短描述商品问题、本人订单物流问题或售后诉求。", "unknown")
        text = text.strip()
        product = self._match_product(context, text)
        policy_question = product is not None and any(hint in text for hint in _POLICY_HINTS)
        action = self._human_action(text)
        if (policy_question and action is not None
                and action[0] in ("return", "exchange", "refund", "after_sales")
                and not any(hint in text for hint in ("申请", "我要", "帮我", "给我", "处理", "坏了", "破损", "漏发", "少件", "投诉"))):
            action = None
        order_question = any(hint in text for hint in ("订单", "物流", "快递", "发货", "到货", "包裹", "运单"))
        order_ids = self._order_ids(text)
        if action is not None or order_question or (order_ids and product is None):
            if context.get("verified_customer") is not True or not _identity(context.get("buyer_id")):
                return _response("查看订单或准备售后草稿前，需要可信渠道核验买家身份；提供订单号本身不能授权。", "identity_required", requires_human=True)
            order = self._owned_order(context, order_ids)
            intent = "after_sales" if action is not None else "order_status"
            if order is None:
                return _response("暂未找到你可查看的唯一订单，请确认本人订单号或联系人工核验。", intent, requires_human=True)
            if action is not None:
                return self._after_sales_reply(order, action)
            return self._order_reply(order)
        if product is not None:
            return self._product_reply(product, text)
        return _response("这是确定性业务演示。请提供商品名称或 SKU，查询本人订单物流，或描述需人工处理的售后诉求。", "unknown")

    def _match_product(self, context: dict, text: str) -> dict | None:
        scoped = [item for item in self._catalog if self._same_shop(item, context)]
        sku_matches = [item for item in scoped if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(item['sku'])}(?![A-Za-z0-9_-])", text, re.IGNORECASE
        )]
        matches = sku_matches or [item for item in scoped if any(
            alias.casefold() in text.casefold() for alias in [item["name"], *item.get("aliases", [])]
        )]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _same_shop(item: dict, context: dict) -> bool:
        return item["tenant_id"] == context["tenant_id"] and item["shop_id"] == context["shop_id"]

    @staticmethod
    def _human_action(text: str) -> tuple[str, str] | None:
        for code, title, hints in _HUMAN_ACTIONS:
            if any(hint in text for hint in hints):
                return code, title
        return None

    @staticmethod
    def _order_ids(text: str) -> list[str]:
        found = [match.group(1) for match in _NAMED_ORDER.finditer(text)]
        found.extend(match.group(0) for match in _ORDER_TOKEN.finditer(text))
        return list(dict.fromkeys(value.casefold() for value in found))

    def _owned_order(self, context: dict, order_ids: list[str]) -> dict | None:
        if len(order_ids) > 1:
            return None
        owned = [item for item in self._orders if self._same_shop(item, context)
                 and item["buyer_id"] == context["buyer_id"]]
        if order_ids:
            owned = [item for item in owned if item["order_id"].casefold() == order_ids[0]]
        return owned[0] if len(owned) == 1 else None

    @staticmethod
    def _source(item: dict, kind: str) -> dict:
        source = {
            "id": item["source_id"] if kind == "knowledge" else f"order:{item['order_id']}",
            "title": item["source_title"] if kind == "knowledge" else f"{item['order_id']} 演示订单记录",
            "kind": kind,
            "tenant_id": item["tenant_id"],
            "shop_id": item["shop_id"],
        }
        source["sku" if kind == "knowledge" else "order_id"] = item["sku" if kind == "knowledge" else "order_id"]
        return source

    def _product_reply(self, item: dict, text: str) -> dict:
        requested = [key for key, hints in _FACT_HINTS.items() if any(hint in text for hint in hints)]
        known = [key for key in requested if key in item["facts"]] if requested else list(item["facts"])
        missing = [key for key in requested if key not in item["facts"]]
        lines = [f"{item['sku']}「{item['name']}」"]
        lines.extend(f"{key}：{item['facts'][key]}" for key in known)
        if missing:
            lines.append(f"当前商品资料未提供{'、'.join(missing)}，需人工核验。")
        lines.append(f"知识来源：[{item['source_id']}]")
        return _response("\n".join(lines), "product", [self._source(item, "knowledge")], bool(missing))

    def _order_reply(self, item: dict) -> dict:
        lines = [f"演示订单 {item['order_id']}：{item['status']}。"]
        labels = {"carrier": "承运商", "tracking_no": "运单号", "last_event": "最新物流", "estimated_delivery": "预计送达"}
        lines.extend(f"{label}：{item['logistics'][key]}" for key, label in labels.items()
                     if item.get("logistics", {}).get(key))
        if not item.get("logistics"):
            lines.append("当前演示记录暂无物流明细。")
        lines.append(f"记录来源：[order:{item['order_id']}]")
        return _response("\n".join(lines), "order_status", [self._source(item, "order")])

    def _after_sales_reply(self, item: dict, action: tuple[str, str]) -> dict:
        code, title = action
        suggestions = {
            "refund": "请人工核验售后条件、退回商品验收结果及退款金额，再由人工处理退款。",
            "return": "请人工核验退货条件，确认退回方式，并在收货验收后决定退款或后续处理。",
            "exchange": "请人工核验换货条件、商品验收结果及可换库存，再安排后续处理。",
            "change_address": "请人工核验新的收货信息、发货状态及承运商限制，再决定能否改址。",
            "cancel_order": "请人工核验支付与履约状态、取消条件及退款需求，再决定后续处理。",
            "after_sales": "请人工核验订单归属、诉求和凭证，再给出售后处理方案。",
        }
        draft = (f"【人工处理草稿｜请求尚未执行】\n订单：{item['order_id']}（{item['status']}）。\n"
                 f"诉求：{title}。\n建议：{suggestions[code]}\n此草稿需人工确认，不代表已执行处理。")
        return _response(draft, "after_sales", [self._source(item, "order")], True)
