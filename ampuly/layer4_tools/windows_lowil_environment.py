"""Pure, bounded environment encoding for an already-authorized lowil launch.

This value proves consistency, never permission or sandbox availability. Values
and native bytes are process-local and must not be serialized into receipts.
ASCII names are a deliberately narrow exact profile; values retain Unicode.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import Mapping


LOWIL_ENVIRONMENT_PROFILE = "lowil-utf16le-ascii-v1"
MAX_ENVIRONMENT_ENTRIES = 512
MAX_ENVIRONMENT_CODE_UNITS = 32767  # Application cap, not a Windows OS limit.
_REFUSAL = "Frozen low-integrity environment is invalid; prepare a new approval"


class FrozenLowilEnvironmentError(ValueError):
    """A fixed, non-secret-bearing exact-environment refusal."""


def _reject() -> None:
    raise FrozenLowilEnvironmentError(_REFUSAL) from None


def canonical_environment_items(
    items: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Validate without coercion/normalization, then use ASCII Windows order."""
    if type(items) is not tuple or len(items) > MAX_ENVIRONMENT_ENTRIES:
        _reject()
    seen: set[str] = set()
    checked: list[tuple[str, str]] = []
    minimum_units = 1 if items else 2
    for pair in items:
        if type(pair) is not tuple or len(pair) != 2:
            _reject()
        name, value = pair
        if type(name) is not str or type(value) is not str or not name:
            _reject()
        minimum_units += len(name) + len(value) + 2
        if minimum_units > MAX_ENVIRONMENT_CODE_UNITS:
            _reject()
        # Retain printable spaces/parentheses, without trimming. ASCII only
        # makes upper() precisely the declared name-comparison policy.
        if any(ord(char) < 32 or ord(char) > 126 or char == "=" for char in name):
            _reject()
        folded = name.upper()
        if folded in seen or "\0" in value:
            _reject()
        seen.add(folded)
        try:
            value.encode("utf-16-le", "strict")
        except UnicodeEncodeError:
            _reject()
        checked.append(pair)
    return tuple(sorted(checked, key=lambda pair: pair[0].upper()))


def encode_environment_items(items: tuple[tuple[str, str], ...]) -> bytes:
    """Return one canonical UTF16LE block, including exactly two final NULs."""
    ordered = canonical_environment_items(items)
    text = "".join(name + "=" + value + "\0" for name, value in ordered)
    text += "\0" if ordered else "\0\0"
    try:
        block = text.encode("utf-16-le", "strict")
    except UnicodeEncodeError:
        _reject()
    if len(block) // 2 > MAX_ENVIRONMENT_CODE_UNITS:
        _reject()
    return block


def _mapping_digest(items: tuple[tuple[str, str], ...]) -> str:
    payload = json.dumps(dict(items), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "strict")).hexdigest()


def _local_path(path: str) -> str:
    if type(path) is not str or not path or len(path) > MAX_ENVIRONMENT_CODE_UNITS:
        _reject()
    if any(ord(char) < 32 for char in path):
        _reject()
    try:
        path.encode("utf-16-le", "strict")
    except UnicodeEncodeError:
        _reject()
    parsed = PureWindowsPath(path)
    if (
        not parsed.is_absolute()
        or len(parsed.drive) != 2
        or parsed.drive[1] != ":"
        or not parsed.drive[0].isascii()
        or not parsed.drive[0].isalpha()
        or str(parsed) != path
        or any(part in {".", ".."} or part.endswith((".", " ")) or ":" in part for part in parsed.parts[1:])
    ):
        _reject()
    return path


@dataclass(frozen=True)
class LowilEnvironmentSeal:
    profile: str
    cwd: str = field(repr=False)
    scratch_directory: str = field(repr=False)
    system_directory: str = field(repr=False)
    items: tuple[tuple[str, str], ...] = field(repr=False)
    block: bytes = field(repr=False)
    mapping_sha256: str
    block_sha256: str
    code_units: int

    @property
    def environment(self) -> dict[str, str]:
        return dict(self.items)

    def descriptor(self) -> dict[str, str | int]:
        return {
            "profile": self.profile,
            "encoding": "utf-16-le",
            "mapping_sha256": self.mapping_sha256,
            "block_sha256": self.block_sha256,
            "code_units": self.code_units,
            "cwd_sha256": hashlib.sha256(self.cwd.encode("utf-8", "strict")).hexdigest(),
            "scratch_sha256": hashlib.sha256(self.scratch_directory.encode("utf-8", "strict")).hexdigest(),
            "system_directory_sha256": hashlib.sha256(self.system_directory.encode("utf-8", "strict")).hexdigest(),
        }

    def verify(self, *, cwd: str) -> None:
        if (
            type(self) is not LowilEnvironmentSeal
            or any(type(value) is not str for value in (
                self.profile, self.cwd, self.scratch_directory,
                self.system_directory, self.mapping_sha256, self.block_sha256,
            ))
            or self.profile != LOWIL_ENVIRONMENT_PROFILE
        ):
            _reject()
        if _local_path(cwd) != self.cwd or _local_path(self.cwd) != self.cwd:
            _reject()
        expected_scratch = str(PureWindowsPath(self.cwd) / ".ampuly-tmp")
        if self.scratch_directory != expected_scratch:
            _reject()
        _local_path(self.system_directory)
        ordered = canonical_environment_items(self.items)
        if ordered != self.items:
            _reject()
        env = dict(ordered)
        if any(env.get(name) != expected_scratch for name in ("TEMP", "TMP", "TMPDIR")):
            _reject()
        if not env.get("PATH") or env["PATH"].split(";", 1)[0] != self.system_directory:
            _reject()
        expected_block = encode_environment_items(ordered)
        if type(self.block) is not bytes or self.block != expected_block:
            _reject()
        if type(self.code_units) is not int or self.code_units != len(expected_block) // 2:
            _reject()
        if self.mapping_sha256 != _mapping_digest(ordered):
            _reject()
        if self.block_sha256 != hashlib.sha256(expected_block).hexdigest():
            _reject()

    def verify_plan(
        self,
        *,
        cwd: str,
        items: tuple[tuple[str, str], ...],
        descriptor: object,
    ) -> None:
        self.verify(cwd=cwd)
        if canonical_environment_items(items) != items or items != self.items:
            _reject()
        expected = self.descriptor()
        if type(descriptor) is not dict or set(descriptor) != set(expected):
            _reject()
        if any(type(descriptor[key]) is not type(value) or descriptor[key] != value for key, value in expected.items()):
            _reject()


def seal_lowil_environment(
    cwd: str, environment: Mapping[str, str], *, system_directory: str
) -> LowilEnvironmentSeal:
    """Pure final-map encoder. Bootstrap decisions belong to the trusted host."""
    checked_cwd = _local_path(cwd)
    checked_system_directory = _local_path(system_directory)
    items = canonical_environment_items(tuple(environment.items()))
    block = encode_environment_items(items)
    seal = LowilEnvironmentSeal(
        profile=LOWIL_ENVIRONMENT_PROFILE,
        cwd=checked_cwd,
        scratch_directory=str(PureWindowsPath(checked_cwd) / ".ampuly-tmp"),
        system_directory=checked_system_directory,
        items=items,
        block=block,
        mapping_sha256=_mapping_digest(items),
        block_sha256=hashlib.sha256(block).hexdigest(),
        code_units=len(block) // 2,
    )
    seal.verify(cwd=cwd)
    return seal
