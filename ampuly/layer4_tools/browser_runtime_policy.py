"""Required synchronous policy hooks for the managed agent browser path.

This checks concrete callable interfaces and applies them; backend labels or
capability declarations never grant access. It does not prove a provider's
request interception implementation: each provider still needs conformance
tests and native validation. No browser, file or account is opened here.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping


REQUIRED_BROWSER_POLICY_METHODS = (
    "set_person_driving",
    "set_egress_hosts",
    "set_download_policy",
    # Both lookup and decider actions use these after admission. An adapter
    # without them must be refused before any account/page observation.
    "fence_writes",
    "writes_fenced",
)


class BrowserRuntimePolicyRefused(RuntimeError):
    def __init__(self, reason: str, method: str) -> None:
        self.reason = reason
        self.method = method
        # Fixed method names only; provider exception text can contain secrets.
        super().__init__(f"{reason}: {method}")


@dataclass(frozen=True)
class BrowserRuntimePolicyHooks:
    methods: Mapping[str, Callable[..., Any]]

    def apply(self, name: str, *args: Any, **kwargs: Any) -> Any:
        method = self.methods[name]
        try:
            result = method(*args, **kwargs)
        except Exception:
            raise BrowserRuntimePolicyRefused("browser_runtime_policy_apply_failed", name) from None
        if inspect.isawaitable(result) or inspect.isasyncgen(result):
            # A wrapped async callable can evade iscoroutinefunction. Do not
            # let a coroutine go un-awaited, and never execute it as a policy.
            if inspect.iscoroutine(result):
                result.close()
            raise BrowserRuntimePolicyRefused("browser_runtime_policy_async", name)
        if result is False:
            raise BrowserRuntimePolicyRefused("browser_runtime_policy_declined", name)
        return result


def require_browser_runtime_policy(runtime: Any) -> BrowserRuntimePolicyHooks:
    """Check every required interface before calling any policy setter.

    Static lookup rejects interfaces invented by permissive __getattr__ mocks
    or proxies. Tests provide explicit hooks just like a production adapter;
    there is no environment flag or test-class-name exemption.
    """
    methods: dict[str, Callable[..., Any]] = {}
    for name in REQUIRED_BROWSER_POLICY_METHODS:
        try:
            declared = inspect.getattr_static(runtime, name, None)
            method = getattr(runtime, name) if declared is not None else None
        except Exception:
            raise BrowserRuntimePolicyRefused("browser_runtime_policy_missing", name) from None
        if not callable(method):
            raise BrowserRuntimePolicyRefused("browser_runtime_policy_missing", name)
        call = getattr(method, "__call__", None)
        if (inspect.iscoroutinefunction(method) or inspect.isasyncgenfunction(method)
                or inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)):
            raise BrowserRuntimePolicyRefused("browser_runtime_policy_async", name)
        methods[name] = method
    return BrowserRuntimePolicyHooks(MappingProxyType(methods))
