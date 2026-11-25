"""Models of the rules in the Revenant Transformation Language.

Rules are data. There is no expression, no function and no code in them
(docs/transformation-language.md). These models reject anything else.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_DEPTH = 16
MAX_EACH_DEPTH = 3
MAX_RULES = 200
NAME = r"[A-Za-z0-9_\-]+"
_POINTER = re.compile(rf"^\$(?:\.{NAME}|\[\d+\])*$")
_NAME = re.compile(rf"^{NAME}$")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

REQUEST_SCOPES = frozenset({"body", "query", "path", "header"})
RESPONSE_SCOPES = frozenset({"body", "status", "request.body", "request.query", "request.path"})
# Never readable or writable by a rule, whatever the adapter declares.
CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HTTP_METHODS = frozenset({"GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS"})
CONVERSIONS = (
    "string", "integer", "number", "boolean", "iso8601_to_epoch_s", "epoch_s_to_iso8601",
    "epoch_ms_to_iso8601", "iso8601_to_epoch_ms", "scale",
)
AUTH_TYPES = ("api_key_header", "api_key_query", "bearer", "basic", "none")
EFFECTS = ("read_only", "state_changing", "unknown")


def is_name(text: str) -> bool:
    """Whether a field name can be addressed by a pointer."""
    return bool(_NAME.match(text))


def check_pointer(pointer: str) -> str:
    if not _POINTER.match(pointer):
        raise ValueError(f"{pointer!r} is not a pointer: use $, .name and [n] only")
    if pointer.count(".") + pointer.count("[") > MAX_DEPTH:
        raise ValueError(f"{pointer!r} is deeper than {MAX_DEPTH} levels")
    return pointer


def split_path(path: str) -> tuple[str, str]:
    """Split ``scope:pointer``. Raises ``ValueError`` if it is not a path."""
    scope, separator, pointer = path.partition(":")
    if not separator or not scope:
        raise ValueError(f"{path!r} is not a path: expected <scope>:<pointer>")
    return scope, check_pointer(pointer)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class When(_Model):
    """A conjunction of predicates. Every key that is present must hold."""

    exists: str | None = None
    equals: tuple[str, Any] | None = None
    in_: tuple[str, list[Any]] | None = Field(default=None, alias="in")
    status_in: list[int] | None = None
    not_: When | None = Field(default=None, alias="not")

    @model_validator(mode="after")
    def _something(self) -> When:
        if not self.model_fields_set:
            raise ValueError("a condition must contain at least one predicate")
        return self

    def paths(self) -> list[str]:
        found = [self.exists] if self.exists else []
        found += [item[0] for item in (self.equals, self.in_) if item]
        return found + (self.not_.paths() if self.not_ else [])


class _Rule(_Model):
    mapping_id: str | None = None
    when: When | None = None

    def sources(self) -> list[str]:
        return list(self.when.paths()) if self.when else []

    def targets(self) -> list[str]:
        return []


class Move(_Rule):
    op: Literal["move"]
    from_: str = Field(alias="from")
    to: str
    required: bool = False

    def sources(self) -> list[str]:
        return [self.from_, *super().sources()]

    def targets(self) -> list[str]:
        return [self.to]


class Const(_Rule):
    op: Literal["const"]
    to: str
    value: Any

    def targets(self) -> list[str]:
        return [self.to]


class Default(_Rule):
    op: Literal["default"]
    to: str
    value: Any

    def targets(self) -> list[str]:
        return [self.to]


class Convert(_Rule):
    op: Literal["convert"]
    from_: str = Field(alias="from")
    to: str
    as_: str = Field(alias="as")
    params: dict[str, Any] = Field(default_factory=dict)
    required: bool = False

    @field_validator("as_")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in CONVERSIONS:
            raise ValueError(f"unknown conversion {value!r}")
        return value

    def sources(self) -> list[str]:
        return [self.from_, *super().sources()]

    def targets(self) -> list[str]:
        return [self.to]


class OnUnmappedDefault(_Model):
    default: Any


class EnumMap(_Rule):
    op: Literal["enum_map"]
    from_: str = Field(alias="from")
    to: str
    map: dict[str, Any]
    on_unmapped: Literal["error", "passthrough"] | OnUnmappedDefault = "error"
    required: bool = False

    def sources(self) -> list[str]:
        return [self.from_, *super().sources()]

    def targets(self) -> list[str]:
        return [self.to]


class Template(_Rule):
    op: Literal["template"]
    to: str
    template: str = Field(max_length=2000)
    vars: dict[str, str]

    @model_validator(mode="after")
    def _placeholders(self) -> Template:
        names = _PLACEHOLDER.findall(self.template)
        for name in names:
            if not is_name(name):
                raise ValueError(f"placeholder {{{name}}} is not a plain name")
            if name not in self.vars:
                raise ValueError(f"placeholder {{{name}}} has no entry in vars")
        if "{" in _PLACEHOLDER.sub("", self.template) or "}" in _PLACEHOLDER.sub(
                "", self.template):
            raise ValueError("unbalanced braces in template")
        return self

    def sources(self) -> list[str]:
        return [*self.vars.values(), *super().sources()]

    def targets(self) -> list[str]:
        return [self.to]


class Each(_Rule):
    op: Literal["each"]
    from_: str = Field(alias="from")
    to: str
    rules: list[Rule] = Field(max_length=MAX_RULES)

    def sources(self) -> list[str]:
        return [self.from_, *super().sources()]

    def targets(self) -> list[str]:
        return [self.to]


class Drop(_Rule):
    op: Literal["drop"]
    path: str

    def targets(self) -> list[str]:
        return [self.path]


Rule = Annotated[
    Move | Const | Default | Convert | EnumMap | Template | Each | Drop,
    Field(discriminator="op"),
]
Each.model_rebuild()
When.model_rebuild()


def check_rule(rule: Any, scopes: frozenset[str], *, target_scope: str | None = None,
               each_depth: int = 0) -> list[str]:
    """Problems with one rule's paths: grammar, scope availability, credential carriers.

    ``scopes`` are the scopes the rule may read. ``target_scope`` is the upstream
    request part being written (``header``, ``query``), if that matters.
    """
    problems: list[str] = []
    for source in rule.sources():
        try:
            scope, pointer = split_path(source)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if scope not in scopes:
            allowed = ", ".join(sorted(scopes))
            problems.append(f"scope {scope!r} cannot be read here (available: {allowed})")
        if scope == "header" and pointer[2:].lower() in CREDENTIAL_HEADERS:
            problems.append(f"{source} is a credential carrier and cannot be read by a rule")
    for target in rule.targets():
        try:
            check_pointer(target)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if target_scope == "header" and target[2:].lower() in CREDENTIAL_HEADERS:
            problems.append(f"header {target[2:]!r} is a credential carrier and cannot be written")
    if isinstance(rule, Each):
        if each_depth + 1 > MAX_EACH_DEPTH:
            problems.append(f"each is nested more than {MAX_EACH_DEPTH} levels deep")
        for inner in rule.rules:
            problems += check_rule(inner, scopes | {"item"}, target_scope=target_scope,
                                   each_depth=each_depth + 1)
    return problems


# ---- document-level blocks that mappings carry -------------------------------------------


class InboundAuth(_Model):
    type: Literal["api_key_header", "api_key_query", "bearer", "basic", "none"]
    header: str | None = None
    param: str | None = None

    @model_validator(mode="after")
    def _carrier(self) -> InboundAuth:
        _check_carrier(self.type, self.header, self.param)
        return self


class OutboundAuth(_Model):
    type: Literal["api_key_header", "api_key_query", "bearer", "basic", "none"]
    secret_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    header: str | None = None
    param: str | None = None

    @model_validator(mode="after")
    def _carrier(self) -> OutboundAuth:
        _check_carrier(self.type, self.header, self.param)
        if self.type != "none" and not self.secret_ref:
            raise ValueError("secret_ref is required: the credential itself is never written here")
        return self


def _check_carrier(kind: str, header: str | None, param: str | None) -> None:
    if kind == "api_key_header" and not (header and is_name(header)):
        raise ValueError("api_key_header needs the header name")
    if kind == "api_key_query" and not (param and is_name(param)):
        raise ValueError("api_key_query needs the parameter name")


class AuthBlock(_Model):
    inbound: InboundAuth
    outbound: OutboundAuth


class ErrorVar(_Model):
    const: Any = None
    from_: str | None = Field(default=None, alias="from")

    @model_validator(mode="after")
    def _one(self) -> ErrorVar:
        if ("const" in self.model_fields_set) == (self.from_ is not None):
            raise ValueError("an error variable has exactly one of const and from")
        return self


class ErrorRule(_Model):
    mapping_id: str | None = None
    when: When
    status: int = Field(ge=400, le=599)
    vars: dict[str, ErrorVar]


class LegacyPaging(_Model):
    page_param: str
    limit_param: str
    first_page: int = 1
    default_limit: int | None = Field(default=None, ge=1)
    max_limit: int | None = Field(default=None, ge=1)


class UpstreamPaging(_Model):
    cursor_param: str
    size_param: str | None = None


class CursorSpec(_Model):
    encoding: Literal["base64_json", "plain"]
    shape: Any

    @model_validator(mode="after")
    def _shape(self) -> CursorSpec:
        allowed = {"$page", "$limit", "$offset"}
        if self.encoding == "plain":
            if self.shape not in allowed:
                raise ValueError("a plain cursor is one of $page, $limit, $offset")
        elif not isinstance(self.shape, dict) or not self.shape or not all(
                is_name(str(key)) and value in allowed for key, value in self.shape.items()):
            raise ValueError("a base64_json cursor is an object of $page, $limit, $offset")
        return self


class PagingResponse(_Model):
    items_from: str
    items_to: str
    has_more_from_presence_of: str | None = None
    has_more_to: str | None = None
    echo: dict[str, Literal["$page", "$limit"]] = Field(default_factory=dict)


class Pagination(_Model):
    mapping_id: str | None = None
    strategy: Literal["computed_cursor", "cursor_cache"]
    legacy: LegacyPaging
    upstream: UpstreamPaging
    cursor: CursorSpec | None = None
    response: PagingResponse

    @model_validator(mode="after")
    def _cursor(self) -> Pagination:
        if self.strategy == "computed_cursor" and self.cursor is None:
            raise ValueError("computed_cursor needs the cursor definition")
        return self
