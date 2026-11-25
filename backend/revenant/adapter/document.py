"""Pydantic models of the Adapter Document. Unknown fields are rejected."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from revenant.transform.model import AuthBlock, ErrorRule, Pagination, Rule

MAX_ROUTES = 200
MAX_RULES = 200
MAX_DOCUMENT_BYTES = 512 * 1024


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Upstream(_Model):
    base_url: str = Field(max_length=2000)


class Side(_Model):
    method: str
    path: str = Field(pattern=r"^/", max_length=500)


class Body(_Model):
    mode: Literal["build", "passthrough"] = "build"
    rules: list[Rule] = Field(default_factory=list, max_length=MAX_RULES)


class RequestSection(_Model):
    path: list[Rule] = Field(default_factory=list, max_length=MAX_RULES)
    query: list[Rule] = Field(default_factory=list, max_length=MAX_RULES)
    headers: list[Rule] = Field(default_factory=list, max_length=MAX_RULES)
    body: Body = Field(default_factory=Body)


class StatusMap(_Model):
    from_: int = Field(alias="from", ge=100, le=599)
    to: int = Field(ge=100, le=599)
    mapping_id: str | None = None


class ResponseSection(_Model):
    status_map: list[StatusMap] = Field(default_factory=list, max_length=MAX_RULES)
    headers: list[Rule] = Field(default_factory=list, max_length=MAX_RULES)
    body: Body = Field(default_factory=Body)
    errors: list[ErrorRule] = Field(default_factory=list, max_length=MAX_RULES)


class Route(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{0,99}$")
    mapping_id: str | None = None
    match: Side
    upstream: Side
    effect: Literal["read_only", "state_changing", "unknown"]
    effect_mapping_id: str | None = None
    request: RequestSection = Field(default_factory=RequestSection)
    response: ResponseSection = Field(default_factory=ResponseSection)
    pagination: Pagination | None = None


class ErrorEnvelope(_Model):
    mapping_id: str | None = None
    rules: list[Rule] = Field(default_factory=list, max_length=50)


class AuthSection(AuthBlock):
    mapping_id: str | None = None


class AdapterDocument(_Model):
    schema_version: Literal["1.0"]
    project_slug: str
    upstream: Upstream
    auth: AuthSection
    error_envelope: ErrorEnvelope = Field(default_factory=ErrorEnvelope)
    routes: list[Route] = Field(max_length=MAX_ROUTES)
