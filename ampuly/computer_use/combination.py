"""Which combination was verified (4.3 X11 / C22): evidence belongs to one composition, never to the next.

A Computer Use run is a combination of executors and a model: the structured
browser (Playwright at a version), the vision grammar (this module at a
digest), the desktop helper (fake or a real build), the grounding model and
the acting model. Evidence taken under one combination says nothing about
another: swapping the model, upgrading Playwright or replacing the helper
invalidates every row verified before (C22, G11). The ledger here is keyed
by the combination's digest, so a passing row cannot be inherited by
accident, and a component change is visible as "no evidence" rather than
as a stale pass.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

EVIDENCE_STATUSES = frozenset({"verified", "failed", "unverified"})

# Rows whose verdict does not depend on which model acted: the browser
# executor, its policies and the lifecycle are the parties. Evidence for them
# transfers between combinations that share the executor (same Playwright
# build, grammar digest, helper and platform) and a different model; a row
# where the model is a party (vision, grounding, budget) never transfers.
EXECUTOR_ROWS = frozenset({"C01", "C02", "C03", "C06", "C07", "C13", "C15", "C18", "C24", "C26", "C27", "C29"})
# What a browser session's safety rests on: the rows a delegation is checked against.
BROWSER_SESSION_ROWS = ("C02", "C26", "C06", "C07", "C13")
PROBE_ADVICE = "run scripts/browser_handoff_probe.py --ledger <product home>/computer_use/combination-evidence.json on this machine"


def default_ledger_path() -> Path:
    """Where the product keeps the durable copy: its own home, never a temp dir or the repo."""
    from ampuly.core.config import get_ampuly_home

    return get_ampuly_home() / "computer_use" / "combination-evidence.json"

from ampuly.computer_use import vision_actions
from ampuly.core.config import replace_with_retry


@dataclass(frozen=True)
class Combination:
    browser_executor: str      # e.g. "playwright-python 1.62.0"
    vision_grammar: str        # digest of vision_actions.py
    desktop_helper: str        # "fake" | "<helper build id>"
    grounding_model: str       # "" when no vision model is used
    acting_model: str          # the model slug driving the turn
    platform: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps({f.name: getattr(self, f.name) for f in fields(self)}, sort_keys=True,
                                         separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    def describe(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @property
    def executor_digest(self) -> str:
        """The combination minus its models: what executor-row evidence is keyed by."""
        return hashlib.sha256(json.dumps({f.name: getattr(self, f.name) for f in fields(self)
                                          if f.name not in {"grounding_model", "acting_model"}}, sort_keys=True,
                                         separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def browser_admission_view(ledger: "CombinationEvidence", combination: Combination) -> dict[str, Any]:
    """What the product can say about running a browser session on this executor: which of the rows a
    session rests on have real-browser evidence for this executor, which failed, which are missing, and
    what to do about a gap. A view for the owner and the audit trail, not a gate that opens by itself."""
    rows = ledger.executor_rows(combination)
    verified = sorted(r for r, s in rows.items() if s == "verified")
    failed = sorted(r for r, s in rows.items() if s == "failed")
    missing = [r for r in BROWSER_SESSION_ROWS if rows.get(r) != "verified"]
    return {
        "executor": combination.executor_digest[:16],
        "browser_executor": combination.browser_executor,
        "platform": combination.platform,
        "verified": verified,
        "failed": failed,
        "missing": missing,
        "admitted": not missing and not failed,
        "advice": "" if not missing and not failed else PROBE_ADVICE,
    }


def browser_build() -> dict[str, Any]:
    """The browser executor as installed: the Playwright package and the Chromium build its driver
    manifest names (the build Playwright downloads and launches). "absent" when not installed."""
    try:
        version = metadata.version("playwright")
    except metadata.PackageNotFoundError:
        return {"playwright": "absent", "chromium_revision": "absent", "chromium_version": "absent"}
    build = {"playwright": version, "chromium_revision": "unknown", "chromium_version": "unknown"}
    try:
        import playwright as _playwright

        manifest = Path(_playwright.__file__).parent / "driver" / "package" / "browsers.json"
        for browser in json.loads(manifest.read_text(encoding="utf-8")).get("browsers", []):
            if browser.get("name") == "chromium":
                build["chromium_revision"] = str(browser.get("revision", "") or "unknown")
                build["chromium_version"] = str(browser.get("browserVersion", "") or "unknown")
    except Exception:  # noqa: BLE001 - a manifest that cannot be read is "unknown", never invented
        pass
    return build


def current_combination(*, acting_model: str, grounding_model: str = "", desktop_helper: str = "none") -> Combination:
    """This process's combination. No desktop helper is wired in the product today ("none"); the Fake
    Helper exists only in the E track and a test that uses it says so explicitly. Computed one way."""
    import platform as _platform
    import sys

    build = browser_build()
    return Combination(
        # The executor is the package and the browser build it launches: a Chromium
        # upgrade under the same Playwright version is a different executor (G11).
        browser_executor=f"playwright-python {build['playwright']} / chromium r{build['chromium_revision']} {build['chromium_version']}",
        vision_grammar=f"vision_actions@{_file_digest(Path(vision_actions.__file__))}",
        desktop_helper=str(desktop_helper or "none"),
        grounding_model=str(grounding_model or ""),
        acting_model=str(acting_model or ""),
        platform=f"{_platform.system()} {_platform.machine()} python{sys.version_info.major}.{sys.version_info.minor}",
    )


@dataclass
class CombinationEvidence:
    """Rows verified per combination digest. In memory here; the durable copy is the caller's."""

    _rows: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def record(self, combination: Combination, row: str, *, status: str, evidence: str, recorded_at: str = "") -> None:
        if status not in EVIDENCE_STATUSES:
            raise ValueError(f"unknown evidence status {status!r}")
        self._rows.setdefault(combination.digest, {})[row] = {
            "status": status, "evidence": evidence[:300], "combination": combination.describe(),
            "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def record_steps(self, combination: Combination, steps: list[dict[str, Any]], step_rows: dict[str, tuple[str, ...]], *,
                     source: str) -> dict[str, str]:
        """Feed a probe run into the ledger (X11): a row is verified only when every step that
        speaks for it passed in every mode the run had; one failure makes the row failed; a step
        that ran in one mode but is missing from another -- a pass that aborted part-way -- also
        makes it failed; a row none of the run's steps speak for is left as it was. Returns {row: status}."""
        outcome: dict[str, str] = {}
        spoke: dict[str, list[str]] = {}
        names: dict[str, set[str]] = {}
        modes = sorted({str(item.get("mode", "") or "") for item in steps})
        ran = {(str(item.get("mode", "") or ""), str(item.get("step", "") or "")) for item in steps}
        for item in steps:
            name = str(item.get("step", "") or "")
            for row in step_rows.get(name, ()):
                ok = bool(item.get("ok"))
                outcome[row] = "failed" if (not ok or outcome.get(row) == "failed") else "verified"
                spoke.setdefault(row, []).append(f"{item.get('mode', '')}:{name}")
                names.setdefault(row, set()).add(name)
        for row in list(outcome):
            missing = [f"{mode}:{name}" for mode in modes for name in sorted(names[row]) if (mode, name) not in ran]
            if missing:
                outcome[row] = "failed"
                spoke[row].extend(f"missing {entry}" for entry in missing)
        for row, status in outcome.items():
            self.record(combination, row, status=status, evidence=f"{source}; steps " + ", ".join(spoke[row]))
        return outcome

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CombinationEvidence":
        rows = dict((payload or {}).get("combinations", {}) or {})
        return cls(_rows={str(digest): {str(row): dict(item) for row, item in dict(items or {}).items()} for digest, items in rows.items()})

    @classmethod
    def load(cls, path: Path | str) -> "CombinationEvidence":
        """The durable copy, or an empty ledger when there is none. An unreadable file is absent, not invented."""
        p = Path(path)
        if not p.is_file():
            return cls()
        try:
            return cls.from_payload(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self, path: Path | str) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_payload(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        replace_with_retry(tmp, p)
        return p

    def executor_rows(self, combination: Combination) -> dict[str, str]:
        """Executor-row statuses this combination inherits from any run on the same executor, whatever the model.
        The latest record per row wins; rows where the model is a party are never inherited."""
        target = combination.executor_digest
        latest: dict[str, tuple[str, str]] = {}
        for items in self._rows.values():
            for row, item in items.items():
                if row not in EXECUTOR_ROWS:
                    continue
                described = dict(item.get("combination", {}) or {})
                try:
                    same = Combination(**described).executor_digest == target
                except TypeError:
                    same = False
                if not same:
                    continue
                stamp = str(item.get("recorded_at", "") or "")
                if row not in latest or stamp >= latest[row][0]:
                    latest[row] = (stamp, str(item.get("status", "") or ""))
        return {row: status for row, (_stamp, status) in latest.items()}

    def summary(self) -> list[dict[str, Any]]:
        """One line per combination: what it is and which rows it has verified or failed."""
        out = []
        for digest, items in self._rows.items():
            first = next(iter(items.values()), {})
            out.append({"digest": digest[:16], "combination": dict(first.get("combination", {}) or {}),
                        "verified": sorted(r for r, i in items.items() if i["status"] == "verified"),
                        "failed": sorted(r for r, i in items.items() if i["status"] == "failed"),
                        "recorded_at": max((str(i.get("recorded_at", "")) for i in items.values()), default="")})
        return sorted(out, key=lambda row: row["recorded_at"], reverse=True)

    def status_of(self, combination: Combination, row: str) -> str:
        """'verified' only for this exact combination; anything else is 'no_evidence' or the recorded status."""
        return self._rows.get(combination.digest, {}).get(row, {}).get("status", "no_evidence")

    def verified_rows(self, combination: Combination) -> list[str]:
        return sorted(row for row, item in self._rows.get(combination.digest, {}).items() if item["status"] == "verified")

    def invalidated_by(self, before: Combination, after: Combination) -> list[str]:
        """Rows that were verified for `before` and carry no evidence for `after`: what a change costs."""
        if before.digest == after.digest:
            return []
        return [row for row in self.verified_rows(before) if self.status_of(after, row) != "verified"]

    def to_payload(self) -> dict[str, Any]:
        return {"combinations": self._rows}
