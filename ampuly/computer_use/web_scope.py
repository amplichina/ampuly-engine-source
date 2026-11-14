"""Which page a browser step may open. Pure; no IO (批量自动化 P2 护栏一).

Two rules, and both of them only ever narrow:

* **Forbidden pages.** Some pages are never a step's business, whatever site was delegated: the account's
  security and password pages, the page that rebinds the phone number, the wallet, the withdrawal page, the
  cashier. Delegating a site was never meant to hand over the account behind it. This rule is unconditional and
  there is deliberately no way to widen it -- a deny list anyone can widen is not a deny list. A refusal is
  visible, and a person can still do that page themselves.

* **An address, not a whole domain.** An entry may carry a path: ``example.com`` is the whole site, exactly as
  every entry has meant until now, and ``example.com/creator`` is that part of it and nothing else. Narrowing is
  **opt in and written down** -- see :func:`effective_scope`. People paste a full address into a host list all
  the time (``https://erp.example.cn/login`` means 「the ERP, start here」, not 「stay on the login page」), so a
  path is honoured only where someone set out to narrow, never because it happened to be in the string.

**Only navigation is judged here.** A page's own images, scripts and XHR keep the host-only rule they have always
had, enforced where they always were. A perfectly ordinary page fetches something like ``/api/account/security``
while doing nothing of the sort, and refusing that would break the site rather than protect the account. What
this module judges is the page the browser is being moved to: the address bar, a redirect, a popup, a link a
decider is about to follow.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

# Schemes that are not a place: a rendered blob, a data URI, the blank page, Chromium's own error page. Refusing
# these would refuse the browser's own bookkeeping, and none of them can reach a site.
EXEMPT_SCHEMES = frozenset({"data", "blob", "about", "chrome-error"})

# Words specific enough to judge on their own, anywhere in the address.
_FORBIDDEN_MARKS = (
    # the account itself
    "security", "password", "passwd", "credential", "two-factor", "twofactor", "authenticator",
    "changephone", "change-phone", "bindphone", "bind-phone", "rebind", "resetphone",
    "deleteaccount", "delete-account", "closeaccount", "close-account", "deactivate",
    "apikey", "api-key", "access-token", "accesstoken", "secretkey", "secret-key",
    "verify-identity", "verifyidentity", "identity-verification", "id-verify", "realname", "real-name",
    # money. 「payment」「balance」「billing」「payout」 are NOT here: as substrings they refuse ordinary work --
    # `/orders?status=payment_pending` is an order list filtered by payment status, `/payments-report` is a
    # report, `/finance/balance-sheet` is an accounting page -- and an order list filtered that way is the core
    # bulk-automation workload. They are judged as whole parts below, where `/pay/confirm` and `/payment` still
    # hit and `payment_pending` does not.
    "wallet", "withdraw", "cashout", "cash-out", "checkout", "cashier",
    "recharge", "topup", "top-up", "bankcard", "bank-card",
    # the same pages in Chinese
    "安全", "密码", "换绑", "改绑", "手机号", "注销账号", "实名", "身份验证", "验证码",
    "钱包", "提现", "提款", "取现", "支付", "付款", "充值", "收银", "余额", "银行卡", "打款",
    "密钥", "凭证",
)

# Words too short or too common to match anywhere, judged only as a whole part of the address. ``/pay/finish``
# is the cashier; ``/company/pay-scale`` is not, and ``/displays`` certainly is not.
#
# Left out on purpose: bare ``verify`` and ``card``. A publishing flow legitimately passes through 「验证提交」
# and 「卡片」 pages, and the accounts they would guard are already covered above by ``verify-identity``,
# ``bankcard`` and their Chinese spellings. A deny list that fires on ordinary work gets switched off.
_FORBIDDEN_PARTS = frozenset({
    "pay", "pwd", "otp", "2fa", "mfa", "sms", "token", "tokens", "kyc", "bank",
    "payment", "payments", "balance", "billing", "payout", "payouts",
})


_PART_SEPARATORS = "/?&=."


def _decoded(text: str) -> str:
    """An address part as a person reads it: percent-decoding undone, lower case.

    Undecodable bytes are left as they came rather than raising -- a URL this module cannot read is still one it
    has to judge, and judging the raw form is what it did before.
    """
    raw = str(text or "")
    try:
        return unquote(raw).lower()
    except Exception:  # noqa: BLE001 - an address that will not decode is judged as written
        return raw.lower()


def _parts(text: str) -> list[str]:
    """The address broken at its own joints: path segments, query names and values, a file extension.

    Deliberately not broken at ``-`` or ``_``. ``/pay/confirm`` is the cashier and ``/company/pay-scale`` is a
    salary table, and the cost of the two mistakes is not the same: a page this list lets through still cannot
    move money by itself, because 「支付」 and ``pay`` are effect words and the click needs approval either way --
    whereas a page it wrongly refuses stops real work, with no way to widen it. So the miss is the cheaper error
    here, and the split is the narrow one.
    """
    cleaned = "".join(ch if ch not in _PART_SEPARATORS else " " for ch in str(text or "").lower())
    return [word for word in cleaned.split() if word]


def forbidden_page(url: str) -> str:
    """The word that makes this page off limits, or ``""``.

    Judged on the path and the query together: a site that puts its security page at ``/settings?tab=security``
    is as much the account as one that puts it at ``/account/security``.
    """
    parts = urlsplit(str(url or ""))
    if (parts.scheme or "").lower() in EXEMPT_SCHEMES:
        return ""
    # Decoded first. A Chinese address arrives percent-encoded from everything except a model typing it by hand:
    # `/%E9%92%B1%E5%8C%85` IS 「钱包」, and the route handler, a Location header and a popup all see that form.
    # Matching the raw string left the whole Chinese half of the list inert on exactly the site-driven routes
    # (a 302, a popup) this rule was added to stop.
    path = _decoded(parts.path)
    for mark in _FORBIDDEN_MARKS:
        if mark in f"{path}?{_decoded(parts.query)}":
            return mark
    # The short words are read in the path only. A query carries `?token=…`, `?verify=…`, `?pay=1` on perfectly
    # ordinary pages -- a share link, a CSRF token, a tab name -- and a word there says nothing about what the
    # page IS. The long marks above are specific enough to mean the page wherever they appear.
    for word in _parts(path):
        if word in _FORBIDDEN_PARTS:
            return word
    return ""


def split_entry(entry: str) -> tuple[str, str]:
    """An allow-list entry as ``(host, path)``. ``path`` is ``""`` when the entry is a whole site.

    Accepts what people write: a scheme, a trailing slash, a leading dot, a port. ``("", "")`` when it is not a
    site name at all -- a caller that gets that should refuse rather than guess.
    """
    text = str(entry or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("?", 1)[0].split("#", 1)[0]
    host, _, path = text.partition("/")
    if "@" in host or not host:
        return "", ""
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    host = host.strip(".")
    trimmed = path.strip("/")
    return (host, f"/{trimmed}" if trimmed else "") if host else ("", "")


def host_of(entry: str) -> str:
    """Just the host part, for the rules that are still host-only (a page's own images, scripts and XHR)."""
    return split_entry(entry)[0]


def _host_matches(host: str, allowed: str) -> bool:
    """The rule every gate has always used: the site itself, or any subdomain of it."""
    return bool(allowed) and (host == allowed or host.endswith("." + allowed))


def entry_allows(host: str, path: str, entry: str) -> bool:
    """Whether one entry covers this address. An entry with no path covers the whole site, as it always has."""
    allowed_host, allowed_path = split_entry(entry)
    if not _host_matches(str(host or "").strip().lower().rstrip("."), allowed_host):
        return False
    if not allowed_path:
        return True
    trimmed = str(path or "").strip("/")
    here = f"/{trimmed}" if trimmed else "/"
    # A prefix has to end at a boundary: /creator does not cover /creatorial.
    return here == allowed_path or here.startswith(allowed_path + "/")


def host_in_scope(host: str, entries: object) -> bool:
    """Whether this host is one the scope names, ignoring any path.

    The rule every gate has always used, for the places that judge a **request** rather than a page -- reading an
    API the flow named, for instance. The page rules (part of a site, the pages nobody opens) deliberately do not
    apply there: an order API legitimately lives at ``/v2/orders/9/payment``.
    """
    clean = str(host or "").strip().lower().rstrip(".")
    return bool(clean) and any(_host_matches(clean, host_of(item)) for item in list(entries or []))


def entry_covers(outer: str, inner: str) -> bool:
    """Whether ``inner`` lies entirely inside ``outer``.

    Used to intersect two lists -- what the order delegated and what a step names -- and keep the narrower side
    of each pair. A whole site covers any address on it; an address covers only itself and what is under it.
    """
    outer_host, outer_path = split_entry(outer)
    inner_host, inner_path = split_entry(inner)
    if not outer_host or not inner_host or not _host_matches(inner_host, outer_host):
        return False
    if not outer_path:
        return True
    if not inner_path:
        return False
    return inner_path == outer_path or inner_path.startswith(outer_path + "/")


def effective_scope(domains: object, paths: object = ()) -> tuple[str, ...]:
    """The scope the gates judge against, built from a host list and an optional list of addresses.

    Narrowing is **opt in and written down**, never inferred. People paste a full address into a host list all
    the time -- ``https://erp.example.cn/login`` means 「the ERP, and this is where you start」, not 「stay on the
    login page」 -- so a path found in ``domains`` is dropped exactly as it always was. A host is narrowed only
    when ``paths`` names an address on it, and then the step reaches those addresses and nothing else on that
    host. Hosts that ``paths`` says nothing about stay whole.
    """
    hosts = [host for item in list(domains or []) if (host := host_of(item))]
    named: dict[str, list[str]] = {}
    for item in list(paths or []):
        host, path = split_entry(item)
        # Exactly the host that was delegated, not a subdomain of it. Narrowing `portal.example.com/orders` under
        # a delegated `example.com` cannot do anything: `example.com` already covers every address on every
        # subdomain, so honouring it would instead throw the parent site away -- the opposite of what it says.
        # The save refuses that pair outright (connector_paths_unlisted), and this keeps the runtime honest too.
        if host and path and host in hosts:
            named.setdefault(host, []).append(f"{host}{path}")
    out: list[str] = []
    for host in hosts:
        out.extend(named.get(host) or [host])
    return tuple(sorted(set(out)))


def navigation_refusal(url: str, entries: object) -> str:
    """``""`` when the browser may be moved to this address, else why not.

    Three answers, in the order that makes the refusal honest: ``off_site`` (nothing delegated covers this host),
    ``off_path`` (the host is delegated but only part of it, and this is not that part), ``forbidden_page`` (the
    address was in scope, and is one of the pages no scope ever includes).
    """
    parts = urlsplit(str(url or ""))
    if (parts.scheme or "").lower() in EXEMPT_SCHEMES:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return "off_site"
    listed = [str(item or "") for item in list(entries or []) if str(item or "").strip()]
    if not listed:
        return "off_site"
    if not any(entry_allows(host, parts.path, entry) for entry in listed):
        # Say which of the two it was, so a refusal tells the owner what to fix.
        return "off_path" if any(_host_matches(host, host_of(entry)) for entry in listed) else "off_site"
    return "forbidden_page" if forbidden_page(url) else ""


# What each refusal means, in the owner's words. The gates raise their own sentences; this is the shared one.
REFUSAL_WORDS = {
    "off_site": "这个网址不在这一步获授权的站点里",
    "off_path": "这个站点只授权了其中一部分，这一页不在里面",
    "forbidden_page": "账号安全、改绑手机、钱包、提现、支付一类的页面永远不由产品代开",
}
