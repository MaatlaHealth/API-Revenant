"""OpenAPI 3.x and Swagger 2.0 parser.

Extracts endpoints and *facts*, each with a locator: the JSON pointer of the
place in the document that supports it. ``$ref`` is resolved inside the
document only; remote and file references are never fetched and are reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
PAGINATION_ROLES = {
    "page": "page", "page_number": "page", "pagenumber": "page", "p": "page",
    "limit": "limit", "per_page": "limit", "page_size": "limit", "pagesize": "limit",
    "size": "limit", "count": "limit",
    "cursor": "cursor", "next_cursor": "cursor", "after": "cursor", "starting_after": "cursor",
    "page_token": "cursor", "next_token": "cursor",
    "offset": "offset", "skip": "offset",
}
JSON_MEDIA_TYPES = ("application/json",)
MAX_OPERATIONS = 500
MAX_FIELDS_PER_SCHEMA = 2000
MAX_SCHEMA_DEPTH = 16
EFFECT_EXTENSION = "x-revenant-effect"


@dataclass(frozen=True)
class ParsedFact:
    fact_type: str
    subject: str
    value: dict[str, Any]
    locator: str


@dataclass
class ParsedEndpoint:
    method: str
    path: str
    operation_id: str | None
    summary: str
    parameters: list[dict[str, Any]] = field(default_factory=list)
    request_schema: dict[str, Any] = field(default_factory=dict)
    response_schemas: dict[str, Any] = field(default_factory=dict)
    facts: list[ParsedFact] = field(default_factory=list)


@dataclass
class ParsedSpec:
    spec_format: str
    title: str
    version: str
    servers: list[Any]
    auth_schemes: dict[str, Any]
    endpoints: list[ParsedEndpoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unresolved_refs: list[str] = field(default_factory=list)


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def join(pointer: str, *tokens: str | int) -> str:
    return pointer + "".join("/" + escape(str(token)) for token in tokens)


class _Document:
    """Resolves local ``$ref`` pointers, detecting cycles and remote references."""

    def __init__(self, root: dict[str, Any], spec: ParsedSpec) -> None:
        self.root = root
        self.spec = spec

    def lookup(self, pointer: str) -> Any:
        node: Any = self.root
        for raw in pointer.lstrip("#").split("/")[1:]:
            token = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and token in node:
                node = node[token]
            elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                node = node[int(token)]
            else:
                raise KeyError(pointer)
        return node

    def follow(
        self, node: Any, pointer: str, trail: tuple[str, ...] = ()
    ) -> tuple[Any, str, tuple[str, ...]]:
        """Follow ``$ref`` chains. Returns (node, pointer of its definition, refs followed).

        An unresolvable, remote or cyclic reference returns ``(None, pointer, trail)``
        after recording why.
        """
        while isinstance(node, dict) and isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if not ref.startswith("#/"):
                self._unresolved(ref, "remote or file reference; not fetched")
                return None, pointer, trail
            if ref in trail:
                self._warn(f"cyclic $ref {ref} at {pointer}; not expanded further")
                return None, pointer, trail
            try:
                target = self.lookup(ref)
            except KeyError:
                self._unresolved(ref, "target does not exist in the document")
                return None, pointer, trail
            trail = (*trail, ref)
            node, pointer = target, ref
        return node, pointer, trail

    def _unresolved(self, ref: str, reason: str) -> None:
        entry = f"{ref} ({reason})"
        if entry not in self.spec.unresolved_refs:
            self.spec.unresolved_refs.append(entry)

    def _warn(self, message: str) -> None:
        if message not in self.spec.warnings:
            self.spec.warnings.append(message)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _schema_type(schema: dict[str, Any]) -> str:
    declared = schema.get("type")
    if isinstance(declared, list):
        return "|".join(str(item) for item in declared)
    if isinstance(declared, str):
        return declared
    if "properties" in schema or "additionalProperties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    if any(key in schema for key in ("oneOf", "anyOf")):
        return "union"
    if "enum" in schema:
        return "enum"
    return "any"


def _leaf_value(schema: dict[str, Any], required: bool) -> dict[str, Any]:
    value: dict[str, Any] = {"type": _schema_type(schema), "required": required}
    for key in ("format", "enum", "default", "nullable", "minimum", "maximum", "pattern"):
        if key in schema:
            value[key] = schema[key]
    return value


def _merged_all_of(doc: _Document, schema: dict[str, Any], pointer: str,
                   trail: tuple[str, ...]) -> dict[str, Any]:
    """Fold ``allOf`` members into one object schema (properties and required)."""
    if "allOf" not in schema or not isinstance(schema["allOf"], list):
        return schema
    merged: dict[str, Any] = {key: value for key, value in schema.items() if key != "allOf"}
    properties: dict[str, Any] = dict(merged.get("properties") or {})
    required: list[str] = list(merged.get("required") or [])
    for index, member in enumerate(schema["allOf"]):
        resolved, _where, _trail = doc.follow(member, join(pointer, "allOf", index), trail)
        if isinstance(resolved, dict):
            resolved = _merged_all_of(doc, resolved, pointer, trail)
            properties.update(resolved.get("properties") or {})
            required.extend(resolved.get("required") or [])
            merged.setdefault("type", resolved.get("type", "object"))
    merged["properties"] = properties
    if required:
        merged["required"] = sorted(set(required))
    return merged


def flatten(
    doc: _Document, schema: Any, pointer: str, prefix: str = "$", required: bool = True,
    trail: tuple[str, ...] = (), depth: int = 0,
    out: list[tuple[str, dict[str, Any], str]] | None = None,
) -> list[tuple[str, dict[str, Any], str]]:
    """List every field of a schema as (path, description, locator)."""
    if out is None:
        out = []
    if len(out) >= MAX_FIELDS_PER_SCHEMA:
        return out
    resolved, where, trail = doc.follow(schema, pointer, trail)
    if not isinstance(resolved, dict):
        if resolved is None and isinstance(schema, dict) and "$ref" in schema:
            out.append((prefix, {"type": "unresolved", "required": required}, pointer))
        return out
    resolved = _merged_all_of(doc, resolved, where, trail)
    out.append((prefix, _leaf_value(resolved, required), where))
    if depth >= MAX_SCHEMA_DEPTH:
        doc._warn(f"schema at {where} is deeper than {MAX_SCHEMA_DEPTH} levels; truncated")
        return out
    properties = resolved.get("properties")
    if isinstance(properties, dict):
        needed = set(resolved.get("required") or [])
        for name in sorted(properties):
            flatten(doc, properties[name], join(where, "properties", name), f"{prefix}.{name}",
                    name in needed, trail, depth + 1, out)
    if "items" in resolved:
        flatten(doc, resolved["items"], join(where, "items"), f"{prefix}[]", True,
                trail, depth + 1, out)
    return out


def expand(doc: _Document, schema: Any, pointer: str, trail: tuple[str, ...] = (),
           depth: int = 0) -> Any:
    """Inline local references so a stored schema is self-contained."""
    resolved, where, trail = doc.follow(schema, pointer, trail)
    if resolved is None:
        return {"x-unresolved-ref": schema.get("$ref")} if isinstance(schema, dict) else schema
    if depth >= MAX_SCHEMA_DEPTH:
        return {"x-truncated": True}
    if isinstance(resolved, dict):
        return {key: expand(doc, value, join(where, key), trail, depth + 1)
                for key, value in resolved.items()}
    if isinstance(resolved, list):
        return [expand(doc, value, join(where, index), trail, depth + 1)
                for index, value in enumerate(resolved)]
    return resolved


def _json_schema_of(content: Any) -> tuple[Any, str | None, list[str]]:
    """Pick the JSON schema out of an OpenAPI 3 ``content`` map."""
    if not isinstance(content, dict):
        return None, None, []
    media_types = sorted(content)
    for media in media_types:
        if media.split(";")[0].strip().lower() in JSON_MEDIA_TYPES or media.endswith("+json"):
            entry = content[media]
            if isinstance(entry, dict) and "schema" in entry:
                return entry["schema"], media, media_types
    return None, None, media_types


def _add_fields(doc: _Document, endpoint: ParsedEndpoint, schema: Any, pointer: str,
                scope: str) -> list[str]:
    paths: list[str] = []
    for path, value, where in flatten(doc, schema, pointer):
        endpoint.facts.append(ParsedFact("field", f"{scope}:{path}", value, where))
        paths.append(path)
    return paths


def _parse_operation(doc: _Document, spec: ParsedSpec, path: str, path_item: dict[str, Any],
                     method: str, operation: dict[str, Any],
                     global_security: Any) -> ParsedEndpoint:
    here = join("#/paths", path, method)
    endpoint = ParsedEndpoint(
        method=method.upper(), path=path,
        operation_id=operation.get("operationId") if isinstance(operation.get("operationId"), str)
        else None,
        summary=str(operation.get("summary") or operation.get("description") or "")[:2000],
    )
    swagger = spec.spec_format == "swagger"

    # ---- parameters (path-level first, operation-level overrides) ---------------
    merged: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
    for owner, base in ((path_item, join("#/paths", path)), (operation, here)):
        for index, raw in enumerate(owner.get("parameters") or []):
            resolved, where, _ = doc.follow(raw, join(base, "parameters", index))
            if isinstance(resolved, dict) and "name" in resolved and "in" in resolved:
                merged[(str(resolved["in"]), str(resolved["name"]))] = (resolved, where)
    body_schema: Any = None
    body_pointer = here
    for (location, name), (parameter, where) in sorted(merged.items()):
        if swagger and location == "body":
            body_schema, body_pointer = parameter.get("schema"), join(where, "schema")
            continue
        schema = parameter.get("schema") if not swagger else parameter
        resolved_schema, _, _ = doc.follow(schema, join(where, "schema"))
        description = _leaf_value(resolved_schema if isinstance(resolved_schema, dict) else {},
                                  bool(parameter.get("required", location == "path")))
        endpoint.parameters.append({"in": location, "name": name, **description})
        fact_type = "header" if location == "header" else "parameter"
        endpoint.facts.append(ParsedFact(fact_type, f"{location}:{name}", description, where))
        role = PAGINATION_ROLES.get(name.lower())
        if location == "query" and role:
            endpoint.facts.append(ParsedFact("pagination", name, {"role": role, **description},
                                             where))

    # ---- request body -----------------------------------------------------------
    if not swagger and isinstance(operation.get("requestBody"), dict):
        body, where, _ = doc.follow(operation["requestBody"], join(here, "requestBody"))
        if isinstance(body, dict):
            schema, media, media_types = _json_schema_of(body.get("content"))
            if schema is not None and media is not None:
                body_schema = schema
                body_pointer = join(where, "content", media, "schema")
            elif media_types:
                endpoint.facts.append(ParsedFact(
                    "unsupported_content", "request", {"media_types": media_types},
                    join(where, "content")))
    if body_schema is not None:
        endpoint.request_schema = expand(doc, body_schema, body_pointer) or {}
        _add_fields(doc, endpoint, body_schema, body_pointer, "request")

    # ---- responses --------------------------------------------------------------
    raw_responses = operation.get("responses")
    responses: dict[Any, Any] = raw_responses if isinstance(raw_responses, dict) else {}
    for status in sorted(responses, key=str):
        response, where, _ = doc.follow(responses[status], join(here, "responses", status))
        if not isinstance(response, dict):
            continue
        code = str(status)
        if swagger:
            schema, schema_pointer, media_types = response.get("schema"), join(where, "schema"), []
        else:
            schema, media, media_types = _json_schema_of(response.get("content"))
            schema_pointer = join(where, "content", media or "", "schema")
        endpoint.facts.append(ParsedFact(
            "status_code", code,
            {"description": str(response.get("description") or "")[:500],
             "has_schema": schema is not None},
            where))
        if schema is not None:
            endpoint.response_schemas[code] = expand(doc, schema, schema_pointer)
            paths = _add_fields(doc, endpoint, schema, schema_pointer, f"response[{code}]")
            if code.isdigit() and int(code) >= 400:
                endpoint.facts.append(ParsedFact("error_shape", code, {"fields": paths},
                                                 schema_pointer))
        elif media_types:
            endpoint.facts.append(ParsedFact(
                "unsupported_content", f"response[{code}]", {"media_types": media_types},
                join(where, "content")))
        for header in sorted(response.get("headers") or {}):
            endpoint.facts.append(ParsedFact(
                "header", f"response[{code}]:{header}", {"required": False},
                join(where, "headers", header)))

    # ---- authentication ---------------------------------------------------------
    security = operation.get("security", global_security)
    if isinstance(security, list):
        if not security:
            endpoint.facts.append(ParsedFact("auth", "none", {"type": "none"},
                                             join(here, "security")))
        for requirement in security:
            for scheme_name in sorted(requirement) if isinstance(requirement, dict) else []:
                scheme = spec.auth_schemes.get(scheme_name)
                if scheme is None:
                    spec.warnings.append(
                        f"{endpoint.method} {path} uses undefined security scheme {scheme_name}")
                    continue
                endpoint.facts.append(ParsedFact("auth", scheme_name, scheme["value"],
                                                 scheme["locator"]))

    # ---- declared effect (evidence for the operation-effect proposal) -------------
    effect = operation.get(EFFECT_EXTENSION)
    if effect in ("read_only", "state_changing", "unknown"):
        endpoint.facts.append(ParsedFact("effect", "declared", {"effect": effect},
                                         join(here, EFFECT_EXTENSION)))
    return endpoint


def _auth_schemes(doc: _Document, root: dict[str, Any], swagger: bool) -> dict[str, Any]:
    if swagger:
        found, base = root.get("securityDefinitions"), "#/securityDefinitions"
    else:
        components = _as_dict(root.get("components"))
        found, base = components.get("securitySchemes"), "#/components/securitySchemes"
    raw = _as_dict(found)
    schemes: dict[str, Any] = {}
    for name in sorted(raw):
        scheme, where, _ = doc.follow(raw[name], join(base, name))
        if isinstance(scheme, dict):
            value = {key: scheme[key] for key in ("type", "in", "name", "scheme", "bearerFormat")
                     if key in scheme}
            schemes[name] = {"value": value, "locator": where}
    return schemes


def parse_spec(root: dict[str, Any]) -> ParsedSpec:
    """Parse an OpenAPI 3.x or Swagger 2.0 document that has already been loaded."""
    swagger = "swagger" in root and "openapi" not in root
    info = _as_dict(root.get("info"))
    spec = ParsedSpec(
        spec_format="swagger" if swagger else "openapi",
        title=str(info.get("title") or "")[:300],
        version=str(info.get("version") or "")[:100],
        servers=[],
        auth_schemes={},
    )
    doc = _Document(root, spec)
    if swagger:
        host = root.get("host")
        if isinstance(host, str):
            declared = root.get("schemes")
            schemes: list[Any] = declared if isinstance(declared, list) else ["https"]
            spec.servers = [f"{scheme}://{host}{root.get('basePath') or ''}" for scheme in schemes]
    else:
        spec.servers = [server.get("url") for server in root.get("servers") or []
                        if isinstance(server, dict) and isinstance(server.get("url"), str)]
    spec.auth_schemes = _auth_schemes(doc, root, swagger)

    base_path = str(root.get("basePath") or "").rstrip("/") if swagger else ""
    paths = root.get("paths")
    if not isinstance(paths, dict):
        spec.warnings.append("document has no paths object")
        return spec
    count = 0
    for path in sorted(paths):
        path_item, _where, _ = doc.follow(paths[path], join("#/paths", path))
        if not isinstance(path_item, dict) or not path.startswith("/"):
            spec.warnings.append(f"path {path!r} skipped: not a path item")
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            count += 1
            if count > MAX_OPERATIONS:
                spec.warnings.append(
                    f"more than {MAX_OPERATIONS} operations; the rest were not imported")
                return spec
            endpoint = _parse_operation(doc, spec, path, path_item, method, operation,
                                        root.get("security"))
            endpoint.path = base_path + path
            spec.endpoints.append(endpoint)
    if not spec.endpoints:
        spec.warnings.append("no operations found")
    return spec
