"""Independent, bounded GET readback for the explicitly selected local sandbox.

This reader never writes a product, uses browser credentials, reads the SQLite
file or follows a redirect. It proves only the selected product's current
state, not request-level exactly-once semantics or a real marketplace sale.
"""
from __future__ import annotations

import json
import math
from typing import Any

from .ego_native_binding import EgoNativeBinding

ORIGIN = 'http://127.0.0.1:8899'
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_PRODUCTS = 1000
_TEXT_FIELDS = ('title', 'subtitle', 'category', 'emoji', 'status')


class EgoProductReadbackRefused(ValueError):
    def __init__(self, code: str = 'ego_product_readback_unavailable'):
        self.code = code
        super().__init__(code)


def _product_id(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 9007199254740991:
        raise EgoProductReadbackRefused('ego_product_id_invalid')
    return value


def parse_products_body(body: bytes, product_id: int) -> dict[str, Any] | None:
    """Validate one readback response; return no unrelated products or fields."""
    _product_id(product_id)
    if type(body) is not bytes or not body or len(body) > MAX_BODY_BYTES:
        raise EgoProductReadbackRefused('ego_product_response_invalid')
    try:
        rows = json.loads(body.decode('utf8'))
    except (ValueError, UnicodeError):
        raise EgoProductReadbackRefused('ego_product_response_invalid') from None
    if type(rows) is not list or len(rows) > MAX_PRODUCTS:
        raise EgoProductReadbackRefused('ego_product_response_invalid')
    selected = None
    seen = set()
    for row in rows:
        if type(row) is not dict:
            raise EgoProductReadbackRefused('ego_product_response_invalid')
        identity = _product_id(row.get('id'))
        if identity in seen:
            raise EgoProductReadbackRefused('ego_product_response_ambiguous')
        seen.add(identity)
        if identity == product_id:
            selected = row
    if selected is None:
        return None
    result = {'id': product_id}
    for name in _TEXT_FIELDS:
        value = selected.get(name)
        if type(value) is not str or len(value.encode('utf8')) > 4096:
            raise EgoProductReadbackRefused('ego_product_response_invalid')
        result[name] = value
    if result['status'] not in {'上架', '下架'}:
        raise EgoProductReadbackRefused('ego_product_response_invalid')
    for name in ('price', 'originalPrice'):
        value = selected.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**9:
            raise EgoProductReadbackRefused('ego_product_response_invalid')
        result[name] = value
    stock = selected.get('stock')
    if type(stock) is not int or not 0 <= stock <= 10**9:
        raise EgoProductReadbackRefused('ego_product_response_invalid')
    result['stock'] = stock
    return result


class EgoSandboxProductReader:
    """No model-supplied endpoint, headers, cookies, proxy or HTTP method."""
    async def __call__(self, binding: EgoNativeBinding, product_id: int) -> dict[str, Any] | None:
        _product_id(product_id)
        if type(binding) is not EgoNativeBinding:
            raise EgoProductReadbackRefused('ego_product_binding_invalid')
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=3.0)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar()) as session:
                async with session.get(ORIGIN + '/api/products',
                        headers={'Accept': 'application/json'}, allow_redirects=False) as response:
                    if (response.status != 200 or response.content_type != 'application/json'
                            or response.content_length is not None and response.content_length > MAX_BODY_BYTES):
                        raise EgoProductReadbackRefused()
                    chunks = []; size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_BODY_BYTES:
                            raise EgoProductReadbackRefused('ego_product_response_invalid')
                        chunks.append(chunk)
                    return parse_products_body(b''.join(chunks), product_id)
        except EgoProductReadbackRefused:
            raise
        except Exception:
            raise EgoProductReadbackRefused() from None
