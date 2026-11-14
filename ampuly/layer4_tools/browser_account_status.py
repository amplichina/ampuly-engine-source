"""Bounded login observations, without permission, lease or business-success authority."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_STATES = {'unverified', 'authenticated', 'login_required', 'challenge', 'unknown'}


def _host(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 253 or not value.isascii():
        raise ValueError('invalid probe host')
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        pass
    if re.fullmatch(r'[0-9.]+', value) or not all(
        re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
        for label in value.split('.')
    ):
        raise ValueError('invalid probe host')
    return value.lower()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError('invalid observation identifier')
    return value


def _timestamp(value: Any) -> float:
    if type(value) not in (int, float) or value < 0 or value > 1_000_000_000_000 or not math.isfinite(value):
        raise ValueError('invalid observation time')
    return float(value)


def _selector(value: Any, *, optional: bool = False) -> str:
    if optional and value == '':
        return ''
    if (not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or '>>' in value or value.lstrip().startswith(('/', '..'))
            or re.match(r'\s*[A-Za-z_][\w-]*\s*=', value)
            or re.search(r':(?:has-text|text(?:-is|-matches)?|nth-match)\s*\(', value, re.I)):
        raise ValueError('invalid probe CSS selector')
    return value.strip()


@dataclass(frozen=True)
class LoginProbePolicy:
    """Trusted server configuration only; selectors are CSS visible markers."""

    site_host: str
    authenticated_selector: str = field(repr=False)
    login_required_selector: str = field(repr=False)
    challenge_selector: str = field(default='', repr=False)
    max_age_sec: int = 60
    policy_id: str = 'login-probe-v1'
    timeout_ms: int = 500

    def __post_init__(self) -> None:
        object.__setattr__(self, 'site_host', _host(self.site_host))
        for name in ('authenticated_selector', 'login_required_selector', 'challenge_selector'):
            object.__setattr__(self, name, _selector(getattr(self, name), optional=name == 'challenge_selector'))
        selectors = [value for value in (self.authenticated_selector, self.login_required_selector, self.challenge_selector) if value]
        if len(selectors) != len(set(selectors)):
            raise ValueError('probe selectors must be distinct')
        if type(self.max_age_sec) is not int or not 1 <= self.max_age_sec <= 300:
            raise ValueError('probe max_age_sec must be between 1 and 300')
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= 2000:
            raise ValueError('probe timeout_ms must be between 1 and 2000')
        if len(_identifier(self.policy_id)) > 64:
            raise ValueError('probe policy_id is too long')

    def digest(self) -> str:
        encoded = json.dumps(self.__dict__, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()


def page_host(url: Any) -> str:
    """Return only a canonical HTTP(S) host, never the address or its credentials."""
    if not isinstance(url, str) or len(url) > 8192:
        return ''
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {'http', 'https'} or parsed.username is not None or parsed.password is not None:
            return ''
        return _host(parsed.hostname)
    except (ValueError, TypeError):
        return ''


def fingerprint_digest(raw: Any) -> str:
    """Hash the existing observation fingerprint; raw URLs never leave this helper."""
    if not isinstance(raw, Mapping) or not page_host(raw.get('url')):
        return ''
    structure, scroll = raw.get('structure'), raw.get('scroll')
    if not isinstance(structure, str) or not structure or len(structure) > 256:
        return ''
    if scroll is not None and (not isinstance(scroll, (list, tuple)) or len(scroll) != 2 or any(
        type(value) not in (int, float) or abs(value) > 1_000_000_000 or not math.isfinite(value) for value in scroll
    )):
        return ''
    payload = {'url': raw['url'], 'structure': structure, 'scroll': scroll}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def interpret_login_flags(authenticated: Any, login_required: Any, challenge: Any = False) -> str:
    flags = (authenticated, login_required, challenge)
    if any(type(value) is not bool for value in flags) or sum(flags) != 1:
        return 'unknown'
    return ('authenticated', 'login_required', 'challenge')[flags.index(True)]


@dataclass(frozen=True)
class LoginProbeEvidence:
    """A service-bound observation. It cannot authorize input or verify a business effect."""

    account_id: str
    resource_id: str
    control_fence: str
    observation_id: str
    observed_at: float
    expires_at: float
    policy_id: str
    policy_digest: str
    site_host: str
    fingerprint: str
    result: str

    def __post_init__(self) -> None:
        for name in ('account_id', 'resource_id', 'control_fence', 'observation_id', 'policy_id'):
            _identifier(getattr(self, name))
        if (_timestamp(self.expires_at) <= _timestamp(self.observed_at) or self.result not in _STATES
                or not _DIGEST.fullmatch(self.policy_digest)
                or (self.fingerprint and not _DIGEST.fullmatch(self.fingerprint))):
            raise ValueError('invalid login evidence')
        _host(self.site_host)

    @classmethod
    def from_probe(cls, policy: LoginProbePolicy, raw: Mapping[str, Any], *, account_id: str,
                   resource_id: str, control_fence: str, observation_id: str, observed_at: float) -> LoginProbeEvidence:
        if not isinstance(policy, LoginProbePolicy):
            raise ValueError('a trusted LoginProbePolicy is required')
        source = raw if isinstance(raw, Mapping) else {}
        flags = source.get('flags')
        flags = flags if isinstance(flags, Mapping) else {}
        result = interpret_login_flags(flags.get('authenticated'), flags.get('login_required'), flags.get('challenge'))
        fingerprint = source.get('fingerprint')
        fingerprint = fingerprint if isinstance(fingerprint, str) and _DIGEST.fullmatch(fingerprint) else ''
        if source.get('result') == 'unverified':
            result = 'unverified'
        elif (source.get('host_matches') is not True or source.get('host') != policy.site_host
              or not fingerprint or source.get('result') != result):
            result = 'unknown'
        observed = _timestamp(observed_at)
        return cls(account_id, resource_id, control_fence, observation_id, observed, observed + policy.max_age_sec,
                   policy.policy_id, policy.digest(), policy.site_host, fingerprint, result)

    def projection(self, *, now: float, policy: LoginProbePolicy, account_id: str,
                   resource_id: str, control_fence: str) -> dict[str, Any]:
        """Project freshness and binding only; the registry remains the control authority."""
        now = _timestamp(now)
        if not isinstance(policy, LoginProbePolicy):
            raise ValueError('a trusted LoginProbePolicy is required')
        state, reason = self.result, 'observed'
        if (account_id, resource_id, control_fence) != (self.account_id, self.resource_id, self.control_fence):
            state, reason = 'unverified', 'binding_changed'
        elif policy.digest() != self.policy_digest:
            state, reason = 'unverified', 'policy_changed'
        elif now < self.observed_at:
            state, reason = 'unknown', 'observation_from_future'
        elif now >= self.expires_at:
            state, reason = 'expired', 'observation_expired'
        return {'state': state, 'reason': reason, 'account_id': self.account_id, 'resource_id': self.resource_id,
                'control_fence': self.control_fence, 'observation_id': self.observation_id,
                'observed_at': self.observed_at, 'expires_at': self.expires_at, 'policy_id': self.policy_id,
                'clock': 'monotonic', 'age_seconds': max(0.0, now - self.observed_at),
                'valid_for_seconds': max(0.0, self.expires_at - now) if reason == 'observed' else 0.0,
                'policy_digest': self.policy_digest, 'site_host': self.site_host, 'fingerprint': self.fingerprint}
