"""Strict server configuration for reviewed CNY text usage, not invoices.

Lives under the existing llm_config.yaml authority/trust boundary. No environment
variable, arbitrary file path, request metadata or provider network lookup.
"""
from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ampuly.core.model_visual_tariff import CNYVisualInputConfig


class CNYTextTariffConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    policy_id: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    endpoint: str = Field(min_length=1, max_length=256)
    credential_env: str = Field(default="", max_length=128)
    billing_binding_id: str = Field(min_length=1, max_length=256)
    billing_epoch: str = Field(min_length=1, max_length=256)
    source_reference: str = Field(min_length=1, max_length=256)
    valid_from: str = Field(min_length=1, max_length=64)
    valid_until: str = Field(min_length=1, max_length=64)
    input_token_limit: int = Field(ge=1, le=10_000_000)
    output_token_limit: int = Field(ge=1, le=1_000_000)
    input_micro_cny_per_million: int = Field(ge=0, le=10**15)
    output_micro_cny_per_million: int = Field(ge=0, le=10**15)
    fixed_fee_micro_cny: int = Field(default=0, ge=0, le=2**53 - 1)
    served_models: list[str] = Field(default_factory=list, max_length=16)
    # Absent means text-only. This is part of the same reviewed parent row,
    # not a wire option, inferred model capability or second price table.
    visual_input: CNYVisualInputConfig | None = None

    @field_validator("policy_id", "revision", "model", "endpoint", "billing_binding_id", "billing_epoch", "source_reference")
    @classmethod
    def clean_text(cls, value):
        if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("tariff identity must be canonical text")
        return value

    @field_validator("endpoint")
    @classmethod
    def exact_endpoint(cls, value):
        endpoint = urlsplit(value)
        if (endpoint.scheme not in {"https", "http"} or not endpoint.hostname
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("tariff requires an explicit credential-free API endpoint")
        return value

    @field_validator("credential_env")
    @classmethod
    def credential_source_name(cls, value):
        if value and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", value):
            raise ValueError("tariff credential source must name an existing environment variable")
        return value

    @field_validator("served_models")
    @classmethod
    def exact_model_aliases(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("tariff aliases must be unique")
        for item in value:
            if not item or len(item) > 256:
                raise ValueError("tariff alias is invalid")
            cls.clean_text(item)
        return value

    @model_validator(mode="after")
    def finite_reviewed_window(self):
        try:
            start = datetime.fromisoformat(self.valid_from)
            end = datetime.fromisoformat(self.valid_until)
        except (ValueError, TypeError):
            raise ValueError("tariff validity requires ISO timestamps") from None
        if (start.utcoffset() is None or end.utcoffset() is None or end <= start
                or (end - start).total_seconds() > 366 * 24 * 60 * 60):
            raise ValueError("tariff validity must be an explicit finite reviewed interval of at most 366 days")
        cost = (self.input_token_limit * self.input_micro_cny_per_million
                + self.output_token_limit * self.output_micro_cny_per_million + 999_999) // 1_000_000
        if not 0 < cost + self.fixed_fee_micro_cny <= 2**53 - 1:
            raise ValueError("tariff must have a finite positive maximum CNY cost")
        return self


def validate_tariff_table(rows) -> list[CNYTextTariffConfig]:
    if type(rows) is not list or len(rows) > 64:
        raise ValueError("server tariff table must contain at most 64 rows")
    clean = []
    seen = set()
    for row in rows:
        raw = row.model_dump(mode="json") if type(row) is CNYTextTariffConfig else row
        item = CNYTextTariffConfig.model_validate(raw)
        key = item.model, item.endpoint, item.credential_env
        if key in seen:
            raise ValueError("server tariff route and credential source must be unambiguous")
        seen.add(key)
        clean.append(item)
    return clean
