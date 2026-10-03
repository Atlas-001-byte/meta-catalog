"""JSON Schema 合法性校验（结构级）。

这里只判定文档是不是 *合法的 JSON Schema*（关键字取值结构合法、内部
``$ref`` 可解析、无递归病态结构），不做实例数据校验。未知关键字一律允许，
与 JSON Schema 的扩展惯例一致。
"""

from __future__ import annotations

import re
from typing import Any

from .errors import SchemaComparisonInvalid

_ALLOWED_TYPES = {
    "null",
    "boolean",
    "object",
    "array",
    "number",
    "string",
    "integer",
}

_NONNEG_INT_KEYWORDS = (
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
)
_NUMBER_KEYWORDS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
)
_STRING_KEYWORDS = ("title", "description", "$comment", "format")


def _is_bool(x: Any) -> bool:
    return isinstance(x, bool)


def _is_schema(x: Any) -> bool:
    return isinstance(x, dict) or _is_bool(x)


def validate_schema(document: Any) -> None:
    """校验整份文档；不合法时抛出 :class:`SchemaComparisonInvalid`。"""
    if not isinstance(document, (dict, bool)):
        raise SchemaComparisonInvalid(
            "候选文档不是合法 JSON Schema：根节点必须是对象或布尔值",
            details={"reason": "root_not_schema"},
        )
    try:
        _walk(document, (), set(), document)
    except _RecursiveRef as exc:
        raise SchemaComparisonInvalid(
            "候选文档不是合法 JSON Schema：$ref 形成无展开的递归环",
            details={"reason": "recursive_ref", "ref": str(exc)},
        )


class _RecursiveRef(Exception):
    pass


def _ref_target(doc: Any, ref: str) -> Any | None:
    """解析文档内 ``#/...`` 引用；非文档内引用或解析失败返回 None。"""
    if not ref.startswith("#"):
        return None
    frag = ref[1:]
    if frag == "" or frag == "/":
        return doc
    if not frag.startswith("/"):
        return None
    cur: Any = doc
    for raw in frag[1:].split("/"):
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if tok not in cur:
                return None
            cur = cur[tok]
        elif isinstance(cur, list):
            try:
                cur = cur[int(tok)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _walk(node: Any, path: tuple[str, ...], active: set, root: Any) -> None:
    if _is_bool(node):
        return
    if not isinstance(node, dict):
        raise SchemaComparisonInvalid(
            f"候选文档不是合法 JSON Schema：{path} 处不是对象或布尔值",
            details={"reason": "node_not_schema", "path": "/" + "/".join(path)},
        )

    # $ref：文档内引用必须可解析且在当前展开栈上不成环。
    ref = node.get("$ref")
    if ref is not None:
        if not isinstance(ref, str) or not ref:
            raise SchemaComparisonInvalid(
                "候选文档不是合法 JSON Schema：$ref 必须是非空字符串",
                details={"reason": "bad_ref", "path": "/" + "/".join(path)},
            )
        if ref.startswith("#"):
            target = _ref_target(root, ref)
            if target is None:
                raise SchemaComparisonInvalid(
                    f"候选文档不是合法 JSON Schema：内部引用 {ref} 无法解析",
                    details={"reason": "unresolvable_ref", "ref": ref},
                )
            key = id(target)
            if key in active:
                raise _RecursiveRef(ref)
            active = active | {key}
            _walk(target, path, active, root)
            active.discard(key)

    _check_keywords(node, path)

    for name, sub in node.get("properties", {}).items():
        _walk(sub, path + ("properties", name), active, root)
    for name, sub in node.get("patternProperties", {}).items():
        try:
            re.compile(name)
        except re.error as exc:
            raise SchemaComparisonInvalid(
                f"候选文档不是合法 JSON Schema：patternProperties 的正则 {name!r} 非法",
                details={"reason": "bad_pattern", "pattern": name},
            ) from exc
        _walk(sub, path + ("patternProperties", name), active, root)
    if "additionalProperties" in node and node["additionalProperties"] is not True:
        ap = node["additionalProperties"]
        if _is_schema(ap) and ap is not True:
            _walk(ap, path + ("additionalProperties",), active, root)
        elif ap not in (True, False):
            raise SchemaComparisonInvalid(
                "候选文档不是合法 JSON Schema：additionalProperties 必须是 Schema 或布尔值",
                details={"reason": "bad_additional_properties"},
            )
    if "items" in node:
        items = node["items"]
        if _is_schema(items):
            _walk(items, path + ("items",), active, root)
        elif isinstance(items, list):  # 兼容 draft-4 元组形式
            for i, sub in enumerate(items):
                _walk(sub, path + ("items", str(i)), active, root)
        else:
            raise SchemaComparisonInvalid(
                "候选文档不是合法 JSON Schema：items 必须是 Schema",
                details={"reason": "bad_items"},
            )
    for kw in ("contains", "not", "propertyNames", "unevaluatedProperties"):
        if kw in node and _is_schema(node[kw]):
            _walk(node[kw], path + (kw,), active, root)
    for kw in ("prefixItems",):
        if kw in node:
            vals = node[kw]
            if not isinstance(vals, list) or not all(_is_schema(x) for x in vals):
                raise SchemaComparisonInvalid(
                    f"候选文档不是合法 JSON Schema：{kw} 必须是 Schema 列表",
                    details={"reason": f"bad_{kw}"},
                )
            for i, sub in enumerate(vals):
                _walk(sub, path + (kw, str(i)), active, root)
    for kw in ("allOf", "anyOf", "oneOf"):
        if kw in node:
            vals = node[kw]
            if not isinstance(vals, list) or not vals or not all(_is_schema(x) for x in vals):
                raise SchemaComparisonInvalid(
                    f"候选文档不是合法 JSON Schema：{kw} 必须是非空 Schema 列表",
                    details={"reason": f"bad_{kw}"},
                )
            for i, sub in enumerate(vals):
                _walk(sub, path + (kw, str(i)), active, root)
    for kw in ("definitions", "$defs"):
        if kw in node:
            defs = node[kw]
            if not isinstance(defs, dict):
                raise SchemaComparisonInvalid(
                    f"候选文档不是合法 JSON Schema：{kw} 必须是对象",
                    details={"reason": f"bad_{kw}"},
                )
            for name, sub in defs.items():
                _walk(sub, path + (kw, name), active, root)


def _check_keywords(node: dict, path: tuple[str, ...]) -> None:
    where = "/" + "/".join(path)

    t = node.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not types or not all(isinstance(x, str) and x in _ALLOWED_TYPES for x in types):
            raise SchemaComparisonInvalid(
                f"候选文档不是合法 JSON Schema：{where} 的 type 非法",
                details={"reason": "bad_type", "path": where},
            )
        if len(set(types)) != len(types):
            raise SchemaComparisonInvalid(
                f"候选文档不是合法 JSON Schema：{where} 的 type 存在重复",
                details={"reason": "dup_type", "path": where},
            )

    if "enum" in node and not isinstance(node["enum"], list):
        raise SchemaComparisonInvalid(
            f"候选文档不是合法 JSON Schema：{where} 的 enum 必须是数组",
            details={"reason": "bad_enum", "path": where},
        )

    req = node.get("required")
    if req is not None:
        if not isinstance(req, list) or not all(isinstance(x, str) for x in req) or not req:
            raise SchemaComparisonInvalid(
                f"候选文档不是合法 JSON Schema：{where} 的 required 必须是非空字符串数组",
                details={"reason": "bad_required", "path": where},
            )
        if len(set(req)) != len(req):
            raise SchemaComparisonInvalid(
                f"候选文档不是合法 JSON Schema：{where} 的 required 存在重复项",
                details={"reason": "dup_required", "path": where},
            )

    props = node.get("properties")
    if props is not None and not isinstance(props, dict):
        raise SchemaComparisonInvalid(
            f"候选文档不是合法 JSON Schema：{where} 的 properties 必须是对象",
            details={"reason": "bad_properties", "path": where},
        )

    for kw in _STRING_KEYWORDS:
        if kw in node and not isinstance(node[kw], str):
            raise SchemaComparisonInvalid(
                f"候选文档不是合法 JSON Schema：{where} 的 {kw} 必须是字符串",
                details={"reason": f"bad_{kw}", "path": where},
            )
    for kw in _NONNEG_INT_KEYWORDS:
        if kw in node:
            v = node[kw]
            if _is_bool(v) or not isinstance(v, int) or v < 0:
                raise SchemaComparisonInvalid(
                    f"候选文档不是合法 JSON Schema：{where} 的 {kw} 必须是非负整数",
                    details={"reason": f"bad_{kw}", "path": where},
                )
    for kw in _NUMBER_KEYWORDS:
        if kw in node and (_is_bool(node[kw]) or not isinstance(node[kw], (int, float))):
            raise SchemaComparisonInvalid(
                f"候选文档不是合法 JSON Schema：{where} 的 {kw} 必须是数值",
                details={"reason": f"bad_{kw}", "path": where},
            )
    if "uniqueItems" in node and not _is_bool(node["uniqueItems"]):
        raise SchemaComparisonInvalid(
            f"候选文档不是合法 JSON Schema：{where} 的 uniqueItems 必须是布尔值",
            details={"reason": "bad_unique_items", "path": where},
        )
