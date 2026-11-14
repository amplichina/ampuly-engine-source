"""Request-local candidate read accounting, with no I/O or authority grant.

Only returned project head/history/table bytes, direct enumeration and decoded
JSON are accounted here. Package/context providers, first JSON decoding, native
limit evidence and process RSS are deliberately outside this narrow budget.
"""
from __future__ import annotations

from typing import Any, Iterator

from .ai_employee_contract import AIEmployeeContractError

MAX_CANDIDATE_READ_BYTES = 32 * 1024 * 1024
MAX_CANDIDATE_READ_ATTEMPTS = 2048
MAX_CANDIDATE_DIRECTORY_ENTRIES = 5000
MAX_CANDIDATE_HISTORY_RECORDS = 10000
MAX_CANDIDATE_JSON_NODES = 250000
MAX_CANDIDATE_JSON_DEPTH = 64
_MESSAGE = "岗位候选读取超过本次范围，请缩小范围或稍后再试。"


class AIEmployeeReadBudget:
    """One server-created budget for both candidate observation passes."""

    __slots__ = ("_bytes_read", "_read_attempts", "_directory_entries",
                 "_history_records", "_json_nodes", "_exhausted")

    def __init__(self) -> None:
        self._bytes_read = 0
        self._read_attempts = 0
        self._directory_entries = 0
        self._history_records = 0
        self._json_nodes = 0
        self._exhausted = False

    @property
    def bytes_read(self) -> int:
        return self._bytes_read

    @property
    def read_attempts(self) -> int:
        return self._read_attempts

    @property
    def directory_entries(self) -> int:
        return self._directory_entries

    @property
    def history_records(self) -> int:
        return self._history_records

    @property
    def json_nodes(self) -> int:
        return self._json_nodes

    def _refuse(self) -> None:
        self._exhausted = True
        raise AIEmployeeContractError("candidate_read_limit", _MESSAGE)

    def _check(self) -> None:
        if self._exhausted:
            self._refuse()

    def _operation(self) -> None:
        self._check()
        if self._read_attempts >= MAX_CANDIDATE_READ_ATTEMPTS:
            self._refuse()
        self._read_attempts += 1

    def begin_read(self, per_file_limit: int) -> int:
        """Reserve an attempt and bound the original provider before body I/O."""
        self._operation()
        remaining = MAX_CANDIDATE_READ_BYTES - self._bytes_read
        if type(per_file_limit) is not int or per_file_limit <= 0 or remaining <= 0:
            self._refuse()
        return min(per_file_limit, remaining)

    def consume_bytes(self, count: int, *, limit: int) -> None:
        self._check()
        if (type(count) is not int or count < 0 or type(limit) is not int or limit <= 0
                or count > limit or count > MAX_CANDIDATE_READ_BYTES - self._bytes_read):
            self._refuse()
        self._bytes_read += count

    def begin_enumeration(self, per_pass_limit: int) -> int:
        """Enumeration also consumes an operation; no unbounded retry scan."""
        self._operation()
        remaining = MAX_CANDIDATE_DIRECTORY_ENTRIES - self._directory_entries
        if type(per_pass_limit) is not int or per_pass_limit <= 0 or remaining <= 0:
            self._refuse()
        return min(per_pass_limit, remaining)

    def consume_entries(self, count: int, *, limit: int) -> None:
        self._check()
        if (type(count) is not int or count < 0 or type(limit) is not int or limit <= 0
                or count > limit or count > MAX_CANDIDATE_DIRECTORY_ENTRIES - self._directory_entries):
            self._refuse()
        self._directory_entries += count

    def consume_history_record(self) -> None:
        self._check()
        if self._history_records >= MAX_CANDIDATE_HISTORY_RECORDS:
            self._refuse()
        self._history_records += 1

    @staticmethod
    def _children(value: Any, depth: int) -> Iterator[tuple[Any, int]]:
        if type(value) is dict:
            for key, item in value.items():
                yield key, depth
                yield item, depth
        else:
            for item in value:
                yield item, depth

    def consume_json(self, value: Any) -> None:
        """Count decoded nodes including keys, before canonical/chain checks.

        Iterators retain at most one frame per depth, not one pending item per
        width. JSON decoding already happened under the per-file byte limit.
        """
        self._check()
        stack = [iter(((value, 0),))]
        while stack:
            try:
                item, depth = next(stack[-1])
            except StopIteration:
                stack.pop()
                continue
            if depth > MAX_CANDIDATE_JSON_DEPTH or self._json_nodes >= MAX_CANDIDATE_JSON_NODES:
                self._refuse()
            self._json_nodes += 1
            if type(item) in (dict, list):
                stack.append(self._children(item, depth + 1))
