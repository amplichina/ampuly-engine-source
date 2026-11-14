"""One optional reviewed visual-input billing contract; no price inference.

Pure validation only: no files, environment, network, model, screenshot,
permit, ledger or dispatch. Existing parent tariff owns the complete billed
input-token cap/rates, route/account, revision, validity and final settlement.
"""
from __future__ import annotations

import base64
import binascii
import struct
import zlib

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CNYVisualInputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: int = Field(ge=1, le=1)
    billing_mode: str
    source_reference: str = Field(min_length=1, max_length=256)
    input_units_include_images: bool
    max_images_per_call: int = Field(ge=1, le=1)
    max_png_bytes: int = Field(ge=45, le=2 * 1024 * 1024)
    max_pixels: int = Field(ge=1, le=4_194_304)
    max_request_bytes: int = Field(ge=1, le=4 * 1024 * 1024)
    mime: str
    encoding: str

    @field_validator("source_reference")
    @classmethod
    def canonical_source(cls, value):
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("visual tariff source must be canonical reviewed text")
        return value

    @model_validator(mode="after")
    def one_reviewed_mode(self):
        if (self.billing_mode != "included_in_billed_prompt_tokens"
                or self.input_units_include_images is not True
                or self.mime != "image/png" or self.encoding != "RGB8"):
            raise ValueError("unsupported or unreviewed visual billing mode")
        return self


def checked_visual_contract(value):
    """Revalidate privileged hosts' model_copy/model_construct values too."""
    if value is None:
        return None
    if type(value) is not CNYVisualInputConfig:
        raise ValueError("visual tariff must be the reviewed strict contract")
    return CNYVisualInputConfig.model_validate(value.model_dump(mode="json"))


def _bad():
    raise ValueError("unsupported or invalid reviewed visual input") from None


def _png(uri, contract):
    prefix = "data:image/png;base64,"
    if type(uri) is not str or not uri.startswith(prefix):
        _bad()
    encoded = uri[len(prefix):]
    if not encoded or len(encoded) > 4 * ((contract.max_png_bytes + 2) // 3):
        _bad()
    try:
        png = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, UnicodeError):
        _bad()
    if (not 45 <= len(png) <= contract.max_png_bytes
            or png[:8] != b"\x89PNG\r\n\x1a\n"):
        _bad()
    position, chunks, compressed, width, height = 8, 0, [], 0, 0
    ended = False
    while position < len(png):
        if ended or position + 12 > len(png) or chunks >= 4096:
            _bad()
        size = struct.unpack(">I", png[position:position + 4])[0]
        end = position + 12 + size
        if end > len(png):
            _bad()
        kind = png[position + 4:position + 8]
        data = png[position + 8:position + 8 + size]
        crc = struct.unpack(">I", png[position + 8 + size:end])[0]
        if zlib.crc32(kind + data) & 0xffffffff != crc:
            _bad()
        if chunks == 0:
            if kind != b"IHDR" or size != 13:
                _bad()
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if (width <= 0 or height <= 0 or width * height > contract.max_pixels
                    or (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0)):
                _bad()
        elif kind == b"IDAT":
            compressed.append(data)
        elif kind == b"IEND":
            if size != 0 or not compressed or end != len(png):
                _bad()
            ended = True
        else:
            # No ancillary metadata, animation, palette, alpha or extra chunks.
            _bad()
        chunks += 1
        position = end
    if not ended:
        _bad()
    # Bounded decompression verifies a real RGB8 scanline stream, not merely a
    # plausible header. At max_pixels the buffer is <=16MiB; no image library,
    # unbounded decompress(), write or metadata interpretation is involved.
    stride = 1 + 3 * width
    expected = stride * height
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(b"".join(compressed), expected + 1)
        if (len(raw) != expected or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail or any(value > 4 for value in raw[::stride])):
            _bad()
    except zlib.error:
        _bad()


def checked_visual_content(message, contract):
    """Validate one JSON-frozen user content and return its image count.

Only inline PNG and text blocks are supported. URL fetches, detail controls,
other modalities and any undeclared paid-feature keys remain refused.
"""
    if contract is None:
        _bad()
    content = message.get("content")
    if (message.get("role") != "user" or type(content) is not list
            or not 1 <= len(content) <= 1024):
        _bad()
    images = 0
    for part in content:
        if type(part) is not dict:
            _bad()
        if part.get("type") == "text":
            if set(part) != {"type", "text"} or type(part["text"]) is not str:
                _bad()
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            if (set(part) != {"type", "image_url"} or type(image) is not dict
                    or set(image) != {"url"}):
                _bad()
            images += 1
            if images > contract.max_images_per_call:
                _bad()
            _png(image["url"], contract)
        else:
            _bad()
    if images != 1:
        _bad()
    return images
