"""企业微信「微信客服」protocol layer for the console -- pure and offline.

Why (policy): the VI business takes customers into 企业微信 and
delivers back there. Of the 企业微信 tools only 微信客服 lets software talk
with an external customer (a personal WeChat user): read what they sent,
reply, send a file. Two rules from the official documentation shape
everything below:

* the enterprise may send only within 48 hours after the customer's last
  message, and at most 5 messages in that window; a new customer message
  opens a new window (发送消息, document path/94677);
* only while the session is 0 新接入待处理 or 1 由智能助手接待 (same page,
  and 分配客服会话 path/94669).

Receiving needs a public callback URL and calling the API needs an
enterprise trusted IP. The owner's PC has neither, so a small relay server
(a later batch) holds the 企业微信 secrets, answers the callback and makes
the calls; the console reaches it over one outbound websocket
(``wecom_relay_protocol``). This module is what both sides share about
微信客服 itself:

1. callback crypto (加解密方案说明 path/90968): signature, AES-256-CBC, URL check;
2. parsing the decrypted ``kf_msg_or_event`` callback and ``sync_msg`` pages (path/94670);
3. request payload builders (data only) for the six calls the console may ask for;
4. the send window, a pure record per (客服账号, 客户);
5. the transport interface the relay client will implement, and the one
   guarded send that goes through it.

No I/O except through an injected transport, no configuration or
environment reads, no database. Nothing here opens a socket.

XML: ``defusedxml`` when it is installed; otherwise ``xml.etree`` behind a
size cap, with every DTD / entity declaration refused before the parser sees
the text. ``XML_BACKEND`` names the one in use (the console's install today
has no defusedxml, so it is ``xml.etree``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import struct
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol, runtime_checkable
from xml.etree.ElementTree import Element
from xml.etree.ElementTree import ParseError as _XmlParseError

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:
    from defusedxml import ElementTree as _xml_parser  # type: ignore[import-not-found,import-untyped]

    XML_BACKEND = "defusedxml"
except ImportError:
    from xml.etree import ElementTree as _xml_parser  # type: ignore[no-redef]

    XML_BACKEND = "xml.etree"


# ── typed errors ──


class WecomKfError(Exception):
    """Base of every error raised here. ``code`` is stable; the text is plain Chinese for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class KfCryptoError(WecomKfError):
    """A callback failed its signature, key, padding, length or receive-id check."""


class KfParseError(WecomKfError):
    """A callback body or an API response is not the shape 企业微信 documents."""


class KfPayloadError(WecomKfError):
    """A request the console was about to make breaks a documented limit."""


class KfApiError(WecomKfError):
    """企业微信 answered with a non-zero errcode: it did not do what was asked."""

    def __init__(self, errcode: int, errmsg: str, *, command: str = "") -> None:
        super().__init__("api_error", f"企业微信拒绝了这次请求（错误码 {errcode}）：{errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg
        self.command = command


# ── 1. callback crypto (加解密方案说明) ──

ENCODING_AES_KEY_LENGTH = 43
PKCS7_BLOCK_BYTES = 32
_RANDOM_PREFIX_BYTES = 16
_LENGTH_FIELD_BYTES = 4
_ENCODING_AES_KEY = re.compile(r"[A-Za-z0-9]{43}")
_TIMESTAMP = re.compile(r"[0-9]{1,20}")
_NONCE = re.compile(r"[A-Za-z0-9]{1,64}")
MAX_CALLBACK_BYTES = 64 * 1024
MAX_ENCRYPTED_CHARS = 2 * MAX_CALLBACK_BYTES


def aes_key(encoding_aes_key: str) -> bytes:
    """AESKey = Base64_Decode(EncodingAESKey + "="): 43 letters or digits -> 32 bytes."""
    text = encoding_aes_key if isinstance(encoding_aes_key, str) else ""
    if not _ENCODING_AES_KEY.fullmatch(text):
        raise KfCryptoError("bad_aes_key", "EncodingAESKey 应是 43 位字母或数字，请到企业微信后台重新复制")
    key = base64.b64decode(text + "=")
    if len(key) != 32:
        raise KfCryptoError("bad_aes_key", "EncodingAESKey 解出来不是 32 字节，请到企业微信后台重新复制")
    return key


def compute_signature(token: str, timestamp: str | int, nonce: str, encrypted: str) -> str:
    """msg_signature = sha1 of token, timestamp, nonce and the ciphertext, sorted and joined; lowercase hex."""
    if not isinstance(token, str) or not token:
        raise KfCryptoError("missing_token", "回调 Token 没有配置，不能核对签名")
    parts = sorted(str(part) for part in (token, timestamp, nonce, encrypted))
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def verify_signature(signature: str, *, token: str, timestamp: str | int, nonce: str, encrypted: str) -> bool:
    """Constant-time comparison of a received msg_signature with the one we compute."""
    if not isinstance(signature, str) or not signature or not isinstance(encrypted, str):
        return False
    expected = compute_signature(token, timestamp, nonce, encrypted)
    return hmac.compare_digest(signature.lower().encode("utf-8"), expected.encode("ascii"))


def _pkcs7_pad(data: bytes) -> bytes:
    count = PKCS7_BLOCK_BYTES - len(data) % PKCS7_BLOCK_BYTES
    return data + bytes([count]) * count


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise KfCryptoError("bad_padding", "回调密文解开后是空的，已拒收")
    count = data[-1]
    if count < 1 or count > PKCS7_BLOCK_BYTES or count > len(data) or data[-count:] != bytes([count]) * count:
        raise KfCryptoError("bad_padding", "回调密文的填充不对，已拒收")
    return data[:-count]


def _cipher(key: bytes) -> Cipher:
    return Cipher(algorithms.AES(key), modes.CBC(key[:16]))


def encrypt(message: str, *, encoding_aes_key: str, receive_id: str, random_prefix: bytes | None = None) -> str:
    """16 random bytes + 4-byte big-endian length + message + receive id, PKCS#7 to 32, AES-256-CBC, base64.

    ``random_prefix`` exists so tests are repeatable; leave it out in real use.
    """
    key = aes_key(encoding_aes_key)
    prefix = os.urandom(_RANDOM_PREFIX_BYTES) if random_prefix is None else bytes(random_prefix)
    if len(prefix) != _RANDOM_PREFIX_BYTES:
        raise KfCryptoError("bad_random", "随机前缀必须是 16 字节")
    if not isinstance(message, str) or not isinstance(receive_id, str):
        raise KfCryptoError("bad_message", "要加密的内容必须是文字")
    body = message.encode("utf-8")
    plain = _pkcs7_pad(prefix + struct.pack(">I", len(body)) + body + receive_id.encode("utf-8"))
    encryptor = _cipher(key).encryptor()
    return base64.b64encode(encryptor.update(plain) + encryptor.finalize()).decode("ascii")


def decrypt(encrypted: str, *, encoding_aes_key: str, receive_id: str) -> str:
    """Decrypt one ciphertext. Every mismatch raises ``KfCryptoError``; partial text is never returned.

    Call it only after the signature has been verified (``verify_url`` and
    ``open_callback`` do both, in that order): an unsigned ciphertext must not
    reach the padding check.
    """
    key = aes_key(encoding_aes_key)
    if not isinstance(receive_id, str):
        raise KfCryptoError("receive_id_mismatch", "回调的接收方和本企业对不上，已拒收")
    if not isinstance(encrypted, str) or not encrypted:
        raise KfCryptoError("bad_ciphertext", "回调里的密文是空的，已拒收")
    if len(encrypted) > MAX_ENCRYPTED_CHARS:
        raise KfCryptoError("too_large", "回调密文太长，已拒收")
    try:
        raw = base64.b64decode(encrypted, validate=True)
    except (binascii.Error, ValueError):
        raise KfCryptoError("bad_base64", "回调密文不是合法的 Base64，已拒收") from None
    if not raw or len(raw) % 16:
        raise KfCryptoError("bad_ciphertext", "回调密文长度不对，已拒收")
    decryptor = _cipher(key).decryptor()
    plain = _pkcs7_unpad(decryptor.update(raw) + decryptor.finalize())
    head = _RANDOM_PREFIX_BYTES + _LENGTH_FIELD_BYTES
    if len(plain) < head:
        raise KfCryptoError("bad_length", "回调明文太短，已拒收")
    (length,) = struct.unpack(">I", plain[_RANDOM_PREFIX_BYTES:head])
    end = head + length
    if end > len(plain):
        raise KfCryptoError("bad_length", "回调明文里写的长度和实际不符，已拒收")
    if not hmac.compare_digest(plain[end:], receive_id.encode("utf-8")):
        raise KfCryptoError("receive_id_mismatch", "回调的接收方和本企业对不上，已拒收")
    try:
        return plain[head:end].decode("utf-8")
    except UnicodeDecodeError:
        raise KfCryptoError("bad_message", "回调明文不是 UTF-8 文字，已拒收") from None


def verify_url(
    *, msg_signature: str, timestamp: str | int, nonce: str, echostr: str,
    token: str, encoding_aes_key: str, receive_id: str,
) -> str:
    """The callback URL check: verify the signature over echostr, then return its plaintext."""
    if not verify_signature(msg_signature, token=token, timestamp=timestamp, nonce=nonce, encrypted=echostr):
        raise KfCryptoError("signature_mismatch", "回调签名对不上，已拒收")
    return decrypt(echostr, encoding_aes_key=encoding_aes_key, receive_id=receive_id)


def open_callback(
    body: str | bytes, *, msg_signature: str, timestamp: str | int, nonce: str,
    token: str, encoding_aes_key: str, receive_id: str,
) -> str:
    """A POSTed callback body -> its decrypted inner XML (signature checked first)."""
    encrypted = _child_text(parse_xml(body), "Encrypt")
    if not encrypted:
        raise KfParseError("missing_encrypt", "回调里没有密文，已拒收")
    if not verify_signature(msg_signature, token=token, timestamp=timestamp, nonce=nonce, encrypted=encrypted):
        raise KfCryptoError("signature_mismatch", "回调签名对不上，已拒收")
    return decrypt(encrypted, encoding_aes_key=encoding_aes_key, receive_id=receive_id)


def build_encrypted_reply(
    message: str, *, token: str, encoding_aes_key: str, receive_id: str,
    timestamp: str | int, nonce: str, random_prefix: bytes | None = None,
) -> str:
    """The documented reply packet: Encrypt, MsgSignature, TimeStamp, Nonce."""
    stamp, once = str(timestamp), str(nonce)
    if not _TIMESTAMP.fullmatch(stamp) or not _NONCE.fullmatch(once):
        raise KfCryptoError("bad_reply", "回包的时间戳或随机串格式不对")
    encrypted = encrypt(message, encoding_aes_key=encoding_aes_key, receive_id=receive_id, random_prefix=random_prefix)
    signature = compute_signature(token, stamp, once, encrypted)
    return (
        f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{stamp}</TimeStamp><Nonce><![CDATA[{once}]]></Nonce></xml>"
    )


# ── 2. parsing ──

# "<!" is only allowed to open a CDATA section or a comment. Anything else
# (DOCTYPE, ENTITY, ELEMENT, ATTLIST, NOTATION) is a declaration; without one
# there are no custom entities to expand and no external resources to fetch.
_DECLARATION = re.compile(rb"<!(?!\[CDATA\[|--)")


def parse_xml(data: str | bytes) -> Element:
    """Parse a small callback XML body with the size cap and the no-declaration rule."""
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raise KfParseError("bad_xml", "回调内容不是文字，已拒收")
    if len(raw) > MAX_CALLBACK_BYTES:
        raise KfParseError("too_large", "回调内容太长，已拒收")
    if _DECLARATION.search(raw):
        raise KfParseError("dtd_refused", "回调内容带了文档类型或实体声明，按安全规则拒收")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise KfParseError("bad_xml", "回调内容不是 UTF-8 文字，已拒收") from None
    try:
        return _xml_parser.fromstring(text)
    except (_XmlParseError, ValueError):
        raise KfParseError("bad_xml", "回调内容不是合法的 XML，已拒收") from None


def _child_text(root: Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


_INTEGER = re.compile(r"-?[0-9]{1,18}")


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER.fullmatch(value.strip()):
        return int(value.strip())
    return default


CALLBACK_TOKEN_TTL_SECONDS = 10 * 60
KF_MSG_OR_EVENT = "kf_msg_or_event"


@dataclass(frozen=True)
class KfCallback:
    """The decrypted callback: "there is something new, fetch it with sync_msg"."""

    event: str
    to_user_name: str = ""
    create_time: int = 0
    token: str = ""
    open_kfid: str = ""

    @property
    def is_msg_or_event(self) -> bool:
        return self.event == KF_MSG_OR_EVENT

    def token_usable(self, now: float) -> bool:
        """The callback Token is good for sync_msg for 10 minutes after the callback."""
        return bool(self.token) and now < self.create_time + CALLBACK_TOKEN_TTL_SECONDS


def parse_kf_callback(xml_text: str | bytes) -> KfCallback:
    root = parse_xml(xml_text)
    msg_type = _child_text(root, "MsgType")
    if msg_type != "event":
        raise KfParseError("not_event", f"这不是事件回调（类型：{msg_type or '空'}）")
    event = _child_text(root, "Event")
    if not event:
        raise KfParseError("missing_event", "事件回调里没有事件名")
    callback = KfCallback(
        event=event,
        to_user_name=_child_text(root, "ToUserName"),
        create_time=_int(_child_text(root, "CreateTime")),
        token=_child_text(root, "Token"),
        open_kfid=_child_text(root, "OpenKfId"),
    )
    if callback.is_msg_or_event and (not callback.token or not callback.open_kfid):
        raise KfParseError("missing_field", "微信客服回调缺少 Token 或客服账号")
    return callback


ORIGIN_CUSTOMER = 3
ORIGIN_SYSTEM_EVENT = 4
ORIGIN_SERVICER = 5
ORIGIN_NAMES = {ORIGIN_CUSTOMER: "customer", ORIGIN_SYSTEM_EVENT: "system_event", ORIGIN_SERVICER: "servicer"}
MEDIA_MSGTYPES = frozenset({"image", "voice", "video", "file"})
# The message types 读取消息 (path/94670) documents with their own structure.
# Anything else -- a type added later, or one documented with msgtype only --
# is kept as kind "other" with its raw JSON, never dropped.
KNOWN_MSGTYPES = frozenset({
    "text", *MEDIA_MSGTYPES, "location", "link", "business_card", "miniprogram", "msgmenu", "channels", "event",
})
EVENT_ENTER_SESSION = "enter_session"
EVENT_MSG_SEND_FAIL = "msg_send_fail"
EVENT_SESSION_STATUS_CHANGE = "session_status_change"
# session_status_change.change_type
SESSION_CHANGE_FROM_POOL = 1
SESSION_CHANGE_TRANSFER = 2
SESSION_CHANGE_END = 3
SESSION_CHANGE_REENTER = 4
# msg_send_fail.fail_type
SEND_FAIL_WINDOW_EXPIRED = 4
SEND_FAIL_SESSION_ENDED = 5
SEND_FAIL_QUOTA_USED = 6
MAX_API_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class InboundKfMessage:
    """One entry of a sync_msg page, flattened. ``raw`` keeps the whole entry."""

    msgid: str
    open_kfid: str
    external_userid: str
    send_time: int
    origin: int
    msgtype: str
    kind: str
    text: str = ""
    menu_id: str = ""
    media_id: str = ""
    servicer_userid: str = ""
    event_type: str = ""
    welcome_code: str = ""
    scene: str = ""
    scene_param: str = ""
    change_type: int = 0
    old_servicer_userid: str = ""
    new_servicer_userid: str = ""
    msg_code: str = ""
    fail_msgid: str = ""
    fail_type: int = 0
    recall_msgid: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def origin_name(self) -> str:
        return ORIGIN_NAMES.get(self.origin, "unknown")

    @property
    def from_customer(self) -> bool:
        return self.origin == ORIGIN_CUSTOMER


def parse_inbound_message(item: Mapping[str, Any]) -> InboundKfMessage:
    if not isinstance(item, Mapping):
        raise KfParseError("bad_message", "消息列表里有一项不是对象")
    msgtype = _text(item.get("msgtype"))
    kind = msgtype if msgtype in KNOWN_MSGTYPES else "other"
    body = item.get(msgtype) if msgtype else None
    body = body if isinstance(body, Mapping) else {}
    extra: dict[str, Any] = {}
    if kind == "text":
        extra = {"text": _text(body.get("content")), "menu_id": _text(body.get("menu_id"))}
    elif kind in MEDIA_MSGTYPES:
        extra = {"media_id": _text(body.get("media_id"))}
    elif kind == "event":
        extra = {
            "event_type": _text(body.get("event_type")),
            "welcome_code": _text(body.get("welcome_code")),
            "scene": _text(body.get("scene")),
            "scene_param": _text(body.get("scene_param")),
            "change_type": _int(body.get("change_type")),
            "old_servicer_userid": _text(body.get("old_servicer_userid")),
            "new_servicer_userid": _text(body.get("new_servicer_userid")),
            "msg_code": _text(body.get("msg_code")),
            "fail_msgid": _text(body.get("fail_msgid")),
            "fail_type": _int(body.get("fail_type")),
            "recall_msgid": _text(body.get("recall_msgid")),
        }
    servicer = _text(item.get("servicer_userid")) or (_text(body.get("servicer_userid")) if kind == "event" else "")
    return InboundKfMessage(
        msgid=_text(item.get("msgid")),
        # Events carry the account and the customer inside the event object.
        open_kfid=_text(item.get("open_kfid")) or (_text(body.get("open_kfid")) if kind == "event" else ""),
        external_userid=_text(item.get("external_userid")) or (_text(body.get("external_userid")) if kind == "event" else ""),
        send_time=_int(item.get("send_time")),
        origin=_int(item.get("origin")),
        msgtype=msgtype,
        kind=kind,
        servicer_userid=servicer,
        raw=dict(item),
        **extra,
    )


def check_api_result(data: Mapping[str, Any] | str | bytes, *, command: str = "") -> dict[str, Any]:
    """A 企业微信 JSON answer -> the dict, or ``KfApiError`` when errcode is not 0.

    The relay client uses this so every transport method either returns an
    errcode-0 answer or raises the typed error carrying errcode and errmsg.
    """
    obj: Any = data
    if isinstance(obj, (bytes, bytearray)):
        if len(obj) > MAX_API_RESPONSE_BYTES:
            raise KfParseError("too_large", "企业微信的回答太长，已拒收")
        try:
            obj = bytes(obj).decode("utf-8")
        except UnicodeDecodeError:
            raise KfParseError("bad_json", "企业微信的回答不是 UTF-8 文字") from None
    if isinstance(obj, str):
        if len(obj) > MAX_API_RESPONSE_BYTES:
            raise KfParseError("too_large", "企业微信的回答太长，已拒收")
        try:
            obj = json.loads(obj)
        except RecursionError:
            raise KfParseError("bad_json", "企业微信的回答层级嵌套太深，已拒收") from None
        except ValueError:
            raise KfParseError("bad_json", "企业微信的回答不是合法的 JSON") from None
    if not isinstance(obj, Mapping):
        raise KfParseError("bad_json", "企业微信的回答不是 JSON 对象")
    # No errcode is not read as success: every 企业微信 answer carries one, so
    # its absence means something between here and 企业微信 went wrong.
    errcode = obj.get("errcode")
    if isinstance(errcode, bool) or not isinstance(errcode, int):
        raise KfParseError("bad_json", "企业微信的回答里没有错误码，或错误码不是数字")
    if errcode != 0:
        raise KfApiError(errcode, _text(obj.get("errmsg")), command=command)
    return dict(obj)


@dataclass(frozen=True)
class SyncMsgPage:
    next_cursor: str
    has_more: bool
    messages: tuple[InboundKfMessage, ...]


def parse_sync_msg_response(data: Mapping[str, Any] | str | bytes) -> SyncMsgPage:
    obj = check_api_result(data, command="sync_msg")
    msg_list = obj.get("msg_list", [])
    if not isinstance(msg_list, list):
        raise KfParseError("bad_json", "拉消息的回答里消息列表不是数组")
    return SyncMsgPage(
        next_cursor=_text(obj.get("next_cursor")),
        has_more=obj.get("has_more") in (1, "1"),
        messages=tuple(parse_inbound_message(item) for item in msg_list),
    )


# ── 3. request payload builders (data only) ──

SYNC_MSG_LIMIT_MAX = 1000
TEXT_MAX_BYTES = 2048
WELCOME_CODE_TTL_SECONDS = 20
_WECOM_ID = re.compile(r"[!-~]{1,256}")
_OPTIONAL_TOKEN = re.compile(r"[!-~]{0,1024}")
_MSGID = re.compile(r"[0-9A-Za-z_-]{1,32}")


def _require_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _WECOM_ID.fullmatch(value):
        raise KfPayloadError("missing_field", f"缺少{what}，或格式不对")
    return value


def _check_msgid(msgid: Any) -> str:
    if not isinstance(msgid, str) or not _MSGID.fullmatch(msgid):
        raise KfPayloadError("bad_msgid", "消息编号只能是 32 个以内的字母、数字、下划线或短横线")
    return msgid


def _check_text(content: Any) -> str:
    if not isinstance(content, str) or not content.strip():
        raise KfPayloadError("empty_text", "要发的文字是空的")
    size = len(content.encode("utf-8"))
    if size > TEXT_MAX_BYTES:
        raise KfPayloadError(
            "text_too_long",
            f"一条文字最多 2048 字节（约 680 个汉字），这条有 {size} 字节，请分开发或写短一些",
        )
    return content


def build_sync_msg_request(
    *, open_kfid: str, cursor: str = "", token: str = "", limit: int = SYNC_MSG_LIMIT_MAX,
    voice_format: int | None = None,
) -> dict[str, Any]:
    """读取消息: pass the callback Token while it is fresh (10 minutes) and the last next_cursor."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SYNC_MSG_LIMIT_MAX:
        raise KfPayloadError("bad_limit", "一次最多拉 1000 条消息，至少 1 条")
    for value, what in ((cursor, "翻页位置"), (token, "回调凭证")):
        if not isinstance(value, str) or not _OPTIONAL_TOKEN.fullmatch(value):
            raise KfPayloadError("bad_field", f"{what}格式不对")
    payload: dict[str, Any] = {"open_kfid": _require_id(open_kfid, "客服账号"), "limit": limit}
    if cursor:
        payload["cursor"] = cursor
    if token:
        payload["token"] = token
    if voice_format is not None:
        if voice_format not in (0, 1) or isinstance(voice_format, bool):
            raise KfPayloadError("bad_field", "语音格式只能是 0（Amr）或 1（Silk）")
        payload["voice_format"] = voice_format
    return payload


def _send_base(touser: str, open_kfid: str, msgtype: str, msgid: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "touser": _require_id(touser, "客户"),
        "open_kfid": _require_id(open_kfid, "客服账号"),
        "msgtype": msgtype,
    }
    if msgid:
        payload["msgid"] = _check_msgid(msgid)
    return payload


def build_send_text(*, touser: str, open_kfid: str, content: str, msgid: str = "") -> dict[str, Any]:
    payload = _send_base(touser, open_kfid, "text", msgid)
    payload["text"] = {"content": _check_text(content)}
    return payload


def build_send_image(*, touser: str, open_kfid: str, media_id: str, msgid: str = "") -> dict[str, Any]:
    payload = _send_base(touser, open_kfid, "image", msgid)
    payload["image"] = {"media_id": _require_id(media_id, "图片素材")}
    return payload


def build_send_file(*, touser: str, open_kfid: str, media_id: str, msgid: str = "") -> dict[str, Any]:
    payload = _send_base(touser, open_kfid, "file", msgid)
    payload["file"] = {"media_id": _require_id(media_id, "文件素材")}
    return payload


def build_send_welcome(*, code: str, content: str, msgid: str = "") -> dict[str, Any]:
    """send_msg_on_event with the enter_session welcome_code.

    The code comes with the enter_session event only when the customer has had
    no welcome in 48 hours and has not written to the account; it can be used
    once, within 20 seconds (``welcome_code_usable``). It does not count
    against the 5-message window.
    """
    payload: dict[str, Any] = {"code": _require_id(code, "欢迎语凭证"), "msgtype": "text"}
    if msgid:
        payload["msgid"] = _check_msgid(msgid)
    payload["text"] = {"content": _check_text(content)}
    return payload


def welcome_code_usable(event_time: float, now: float) -> bool:
    return now - event_time < WELCOME_CODE_TTL_SECONDS


SERVICE_STATE_NEW = 0
SERVICE_STATE_ASSISTANT = 1
SERVICE_STATE_QUEUED = 2
SERVICE_STATE_HUMAN = 3
SERVICE_STATE_ENDED = 4
SENDABLE_SERVICE_STATES = frozenset({SERVICE_STATE_NEW, SERVICE_STATE_ASSISTANT})
SERVICE_STATE_WORDS = {
    SERVICE_STATE_NEW: "新接入，还没人处理",
    SERVICE_STATE_ASSISTANT: "由智能助手接待",
    SERVICE_STATE_QUEUED: "在排队等人工接待",
    SERVICE_STATE_HUMAN: "由人工接待",
    SERVICE_STATE_ENDED: "已结束或还没开始",
}


def build_service_state_get(*, open_kfid: str, external_userid: str) -> dict[str, Any]:
    return {"open_kfid": _require_id(open_kfid, "客服账号"), "external_userid": _require_id(external_userid, "客户")}


def build_service_state_trans(
    *, open_kfid: str, external_userid: str, service_state: int, servicer_userid: str = "",
) -> dict[str, Any]:
    """变更会话状态. Moving to 3 (转人工) needs the servicer's userid.

    Which moves are allowed from which state is 企业微信's own table; a refused
    move comes back as ``KfApiError``. Moving back to 0 is never asked for.
    """
    if isinstance(service_state, bool) or service_state not in (1, 2, 3, 4):
        raise KfPayloadError("bad_state", "会话只能改成 1 智能助手、2 排队、3 人工 或 4 结束")
    payload = build_service_state_get(open_kfid=open_kfid, external_userid=external_userid)
    payload["service_state"] = service_state
    if service_state == SERVICE_STATE_HUMAN:
        payload["servicer_userid"] = _require_id(servicer_userid, "接待人员（转人工时必须指定）")
    elif servicer_userid:
        payload["servicer_userid"] = _require_id(servicer_userid, "接待人员")
    return payload


def build_transfer_to_human(*, open_kfid: str, external_userid: str, servicer_userid: str) -> dict[str, Any]:
    return build_service_state_trans(
        open_kfid=open_kfid, external_userid=external_userid,
        service_state=SERVICE_STATE_HUMAN, servicer_userid=servicer_userid,
    )


@dataclass(frozen=True)
class ServiceState:
    state: int
    servicer_userid: str = ""

    @property
    def sendable(self) -> bool:
        return self.state in SENDABLE_SERVICE_STATES

    @property
    def words(self) -> str:
        return SERVICE_STATE_WORDS.get(self.state, "状态不明")


def parse_service_state_get_response(data: Mapping[str, Any] | str | bytes) -> ServiceState:
    obj = check_api_result(data, command="service_state_get")
    state = obj.get("service_state")
    if isinstance(state, bool) or not isinstance(state, int):
        raise KfParseError("bad_json", "查会话状态的回答里没有状态值")
    return ServiceState(state=state, servicer_userid=_text(obj.get("servicer_userid")))


def parse_service_state_trans_response(data: Mapping[str, Any] | str | bytes) -> str:
    """The msg_code (for send_msg_on_event) when 企业微信 returns one, else ""."""
    return _text(check_api_result(data, command="service_state_trans").get("msg_code"))


MEDIA_MIN_BYTES_EXCLUSIVE = 5
_MB = 1024 * 1024
MEDIA_MAX_BYTES = {"image": 10 * _MB, "voice": 2 * _MB, "video": 10 * _MB, "file": 20 * _MB}
MEDIA_ID_TTL_SECONDS = 3 * 24 * 60 * 60
UPLOADABLE_MEDIA_TYPES = ("image", "file")
IMAGE_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_FILENAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f/\\]")


@dataclass(frozen=True)
class MediaUploadRequest:
    """What the relay needs to upload one temporary material (上传临时素材, path/90253).

    The media_id that comes back is valid for 3 days.
    """

    media_type: str
    filename: str
    size: int
    content_type: str
    sha256: str = ""

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.media_type, "filename": self.filename, "size": self.size, "content_type": self.content_type,
        }
        if self.sha256:
            payload["sha256"] = self.sha256
        return payload


def describe_media_upload(
    *, media_type: str, filename: str, size: int, content_type: str = "", sha256: str = "",
) -> MediaUploadRequest:
    if media_type not in UPLOADABLE_MEDIA_TYPES:
        raise KfPayloadError("bad_media_type", "这里只能上传图片或文件")
    if not isinstance(filename, str) or not filename.strip() or len(filename) > 255 or _FILENAME_FORBIDDEN.search(filename):
        raise KfPayloadError("bad_filename", "文件名不能为空、不能带路径，也不能超过 255 个字符")
    if isinstance(size, bool) or not isinstance(size, int) or size <= MEDIA_MIN_BYTES_EXCLUSIVE:
        raise KfPayloadError("too_small", "文件太小（不超过 5 字节），企业微信不收")
    suffix = os.path.splitext(filename)[1].lower()
    if media_type == "image":
        if suffix not in IMAGE_CONTENT_TYPES:
            raise KfPayloadError("bad_image_format", "图片只能是 JPG 或 PNG 格式")
        if size > MEDIA_MAX_BYTES["image"]:
            raise KfPayloadError("too_large", "图片超过 10MB，企业微信发不了，请压缩后再发")
        default_type = IMAGE_CONTENT_TYPES[suffix]
    else:
        if size > MEDIA_MAX_BYTES["file"]:
            raise KfPayloadError("too_large", "文件超过 20MB，企业微信发不了，请压缩或拆成几份再发")
        default_type = "application/octet-stream"
    if sha256 and (not isinstance(sha256, str) or not _SHA256_HEX.fullmatch(sha256)):
        raise KfPayloadError("bad_field", "文件校验值格式不对")
    kind = content_type if isinstance(content_type, str) and content_type.strip() else default_type
    return MediaUploadRequest(media_type=media_type, filename=filename, size=size, content_type=kind, sha256=sha256)


# ── 4. the send window ──

SEND_WINDOW_SECONDS = 48 * 60 * 60
SEND_QUOTA_PER_WINDOW = 5
SEND_OK = "ok"
SEND_WINDOW_CLOSED = "window_closed"
SEND_QUOTA_USED = "quota_used"
SEND_HUMAN_HANDLING = "human_handling"

_WORDS_NEVER_MESSAGED = "客户还没在微信客服里给我们发过消息，系统不能主动发，要等客户先发一条"
_WORDS_WINDOW_CLOSED = "客户最后一次发消息已经超过 48 小时，系统不能再主动发了，请在企业微信里手动转给客户"
_WORDS_QUOTA_USED = "客户上次发消息之后系统已经发了 5 条，企业微信不让再发了，要等客户回一句才能继续"
_WORDS_BY_STATE = {
    SERVICE_STATE_QUEUED: "这位客户正在排队等人工接待，系统不再代发，请在企业微信里接入后回复",
    SERVICE_STATE_HUMAN: "这位客户已经转给人工接待，系统不再代发，请在企业微信里直接回复",
    SERVICE_STATE_ENDED: "这次会话已经结束，系统不能再发，要等客户重新发消息",
}
_WORDS_OTHER_STATE = "这位客户的会话现在不归系统接待，系统不再代发，请在企业微信里处理"


@dataclass(frozen=True)
class SendDecision:
    reason: str
    message: str
    remaining: int = 0
    window_closes_at: float | None = None

    @property
    def ok(self) -> bool:
        return self.reason == SEND_OK


@dataclass(frozen=True)
class KfSendWindow:
    """What the console knows about sending to one customer through one 客服账号.

    Immutable: every ``record_*`` / ``apply`` returns a new record. The state
    is a local copy; 企业微信 is the authority, and the wiring batch refreshes
    it with service_state/get before acting on it. Local updates only ever
    move toward "do not send" (human, ended, quota used) -- a customer message
    after a human ended the session does not reopen it here; the refresh does.
    """

    open_kfid: str
    external_userid: str
    last_customer_message_at: float | None = None
    sent_since_customer_message: int = 0
    service_state: int = SERVICE_STATE_NEW

    def record_customer_message(self, at: float) -> KfSendWindow:
        """A newer customer message opens a new window: time moves, the count goes back to 0.

        A message no newer than the one on record (a repeat delivery, or an
        older one arriving late) changes nothing.
        """
        last = self.last_customer_message_at
        if last is not None and at <= last:
            return self
        return replace(self, last_customer_message_at=float(at), sent_since_customer_message=0)

    def record_sent(self) -> KfSendWindow:
        return replace(self, sent_since_customer_message=self.sent_since_customer_message + 1)

    def with_service_state(self, state: int) -> KfSendWindow:
        return replace(self, service_state=state)

    def can_send(self, now: float) -> SendDecision:
        state = self.service_state
        if state not in SENDABLE_SERVICE_STATES:
            return SendDecision(SEND_HUMAN_HANDLING, _WORDS_BY_STATE.get(state, _WORDS_OTHER_STATE))
        last = self.last_customer_message_at
        if last is None:
            return SendDecision(SEND_WINDOW_CLOSED, _WORDS_NEVER_MESSAGED)
        closes_at = last + SEND_WINDOW_SECONDS
        if now >= closes_at:
            return SendDecision(SEND_WINDOW_CLOSED, _WORDS_WINDOW_CLOSED, window_closes_at=closes_at)
        sent = max(0, self.sent_since_customer_message)
        if sent >= SEND_QUOTA_PER_WINDOW:
            return SendDecision(SEND_QUOTA_USED, _WORDS_QUOTA_USED, window_closes_at=closes_at)
        remaining = SEND_QUOTA_PER_WINDOW - sent
        return SendDecision(
            SEND_OK, f"可以发，客户这次发消息之后系统还能再发 {remaining} 条",
            remaining=remaining, window_closes_at=closes_at,
        )

    def apply(self, message: InboundKfMessage) -> KfSendWindow:
        """Fold one synced message into the record (messages for another pair change nothing)."""
        if message.open_kfid != self.open_kfid or message.external_userid != self.external_userid:
            return self
        if message.kind == "event":
            if message.event_type == EVENT_SESSION_STATUS_CHANGE:
                if message.change_type == SESSION_CHANGE_END:
                    return self.with_service_state(SERVICE_STATE_ENDED)
                if message.change_type in (SESSION_CHANGE_FROM_POOL, SESSION_CHANGE_TRANSFER, SESSION_CHANGE_REENTER):
                    return self.with_service_state(SERVICE_STATE_HUMAN)
            elif message.event_type == EVENT_MSG_SEND_FAIL:
                if message.fail_type == SEND_FAIL_QUOTA_USED:
                    return replace(
                        self, sent_since_customer_message=max(self.sent_since_customer_message, SEND_QUOTA_PER_WINDOW),
                    )
                if message.fail_type == SEND_FAIL_SESSION_ENDED:
                    return self.with_service_state(SERVICE_STATE_ENDED)
            return self
        if message.from_customer:
            return self.record_customer_message(message.send_time)
        return self


# ── 5. transport ──

# The six calls the console may ask the relay to make, and the 企业微信 path
# each maps to (the relay adds host and access_token; the console never calls them).
KF_COMMANDS = (
    "sync_msg", "send_msg", "send_msg_on_event", "upload_media", "service_state_get", "service_state_trans",
)
KF_COMMAND_PATHS = {
    "sync_msg": "/cgi-bin/kf/sync_msg",
    "send_msg": "/cgi-bin/kf/send_msg",
    "send_msg_on_event": "/cgi-bin/kf/send_msg_on_event",
    "upload_media": "/cgi-bin/media/upload",
    "service_state_get": "/cgi-bin/kf/service_state/get",
    "service_state_trans": "/cgi-bin/kf/service_state/trans",
}


@runtime_checkable
class KfTransport(Protocol):
    """What the relay client (a later batch) implements.

    Each method returns the parsed errcode-0 JSON answer, or raises
    ``KfApiError`` carrying errcode/errmsg (``check_api_result`` does that).
    Any other exception means the outcome is unknown.
    """

    async def sync_msg(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def send_msg(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def send_msg_on_event(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def upload_media(self, request: MediaUploadRequest, content: bytes) -> dict[str, Any]: ...

    async def service_state_get(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def service_state_trans(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...


class KfSendRefused(WecomKfError):
    """The send window said no; nothing was sent."""

    def __init__(self, decision: SendDecision) -> None:
        super().__init__(decision.reason, decision.message)
        self.decision = decision


class KfSendOutcomeUnknown(WecomKfError):
    """The transport failed without an answer from 企业微信: the message may or may not have gone.

    ``window`` has it counted -- better one message too few than a sixth one.
    """

    def __init__(self, window: KfSendWindow) -> None:
        super().__init__(
            "outcome_unknown",
            "发送结果不确定（中转服务没有给回音），已按发出一条计数，请到企业微信里核对客户是否收到",
        )
        self.window = window


async def send_checked(
    transport: KfTransport, window: KfSendWindow, payload: Mapping[str, Any], *, now: float,
) -> tuple[KfSendWindow, dict[str, Any]]:
    """The one way to call send_msg: the window decides first, then the message is counted.

    企业微信 refusing (``KfApiError``) leaves the count alone -- nothing went.
    Any other failure is an unknown outcome and is counted.
    """
    if payload.get("touser") != window.external_userid or payload.get("open_kfid") != window.open_kfid:
        raise KfPayloadError("wrong_recipient", "要发的客户和这条发送记录对不上，已停下")
    decision = window.can_send(now)
    if not decision.ok:
        raise KfSendRefused(decision)
    counted = window.record_sent()
    try:
        result = await transport.send_msg(dict(payload))
    except KfApiError:
        raise
    except Exception as exc:
        raise KfSendOutcomeUnknown(counted) from exc
    return counted, result
