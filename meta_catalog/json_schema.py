"""JSON Schema 校验、解析与扁平化（支持常用子集）。

支持的关键字子集：
``type`` / ``properties`` / ``patternProperties``(仅校验) / ``required`` /
``items`` / ``prefixItems`` / ``enum`` / ``const`` / ``default`` /
``format`` / ``pattern`` / 数值与长度边界 / ``allOf`` /
``$ref``（本地与跨 Schema）/ ``$defs`` / ``definitions`` /
``title`` / ``description`` / ``$comment``。

字段路径使用 JSON Pointer 风格：根为 ``""``，属性间以 ``/`` 分隔，
``~`` 转义为 ``~0``，``/`` 转义为 ``~1``；数组元素形状用 ``[]`` 表示，
元组用 ``/0``、``/1`` 下标表示。

跨 Schema 引用形式：``catalog://<名称>@<版本>#<Pointer>``，
``@<版本>`` 与 ``#<Pointer>`` 均可省略。
"""

from __future__ import annotations

import json
from typing import Any

from .errors import CatalogError, ErrorCode
from .limits import LIMITS

ALLOWED_TYPES = {
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
}

# 元数据关键字（只调整这些归为 metadata 变更）
META_KEYS = ("title", "description", "$comment")
NUMBER_CONSTRAINTS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
# 取值为 Schema 对象映射的关键字
SCHEMA_MAP_KEYWORDS = (
    "properties",
    "patternProperties",
    "$defs",
    "definitions",
    "dependentSchemas",
)
LENGTH_CONSTRAINTS = ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties")


class SchemaShapeError(ValueError):
    """文档不是合法 JSON Schema（内部异常，由调用方映射公开错误码）。"""


def parse_json(raw: str | bytes | dict | list | bool) -> Any:
    """解析 JSON；已是 Python 对象时原样返回。"""
    if isinstance(raw, (dict, list, bool)):
        return raw
    if isinstance(raw, (str, bytes)):
        if isinstance(raw, str) and len(raw.encode("utf-8")) > LIMITS["max_schema_bytes"]:
            raise SchemaShapeError("schema document exceeds size limit")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SchemaShapeError(f"invalid JSON document: {exc}") from exc
    raise SchemaShapeError("schema document must be JSON object or string")


def validate(doc: Any) -> None:
    """递归校验文档是否为合法 JSON Schema（布尔 Schema 合法）。"""
    if not isinstance(doc, (dict, bool)):
        raise SchemaShapeError("JSON Schema root must be an object or boolean")
    _validate_node(doc, frozenset())


def _validate_node(node: Any, seen: frozenset[str]) -> None:
    if isinstance(node, bool):
        return
    if not isinstance(node, dict):
        raise SchemaShapeError("schema node must be object or boolean")

    if "$schema" in node and not isinstance(node["$schema"], str):
        raise SchemaShapeError("$schema must be a string")

    if "type" in node:
        types = node["type"]
        if isinstance(types, str):
            types = [types]
        if not isinstance(types, list) or not types:
            raise SchemaShapeError("type must be a string or non-empty array")
        bad = [t for t in types if not isinstance(t, str) or t not in ALLOWED_TYPES]
        if bad:
            raise SchemaShapeError(f"unknown type values: {bad!r}")

    for key in ("enum",):
        if key in node:
            if not isinstance(node[key], list) or not node[key]:
                raise SchemaShapeError(f"{key} must be a non-empty array")

    if "required" in node:
        req = node["required"]
        if not isinstance(req, list) or any(not isinstance(r, str) for r in req):
            raise SchemaShapeError("required must be an array of strings")

    for key in ("format", "pattern", "title", "description", "$comment"):
        if key in node and not isinstance(node[key], str):
            raise SchemaShapeError(f"{key} must be a string")

    for key in ("default", "const"):
        # default/const 可以是任意 JSON 值
        pass

    for key in NUMBER_CONSTRAINTS:
        if key in node and (not isinstance(node[key], (int, float)) or isinstance(node[key], bool)):
            raise SchemaShapeError(f"{key} must be a number")
    for key in LENGTH_CONSTRAINTS:
        if key in node and (not isinstance(node[key], int) or isinstance(node[key], bool)):
            raise SchemaShapeError(f"{key} must be an integer")

    for key in SCHEMA_MAP_KEYWORDS:
        if key in node:
            val = node[key]
            if not isinstance(val, dict) or any(not isinstance(k, str) or not isinstance(v, (dict, bool)) for k, v in val.items()):
                raise SchemaShapeError(f"{key} must be an object mapping names to schemas")

    for key in ("not", "contains", "additionalProperties", "propertyNames", "additionalItems", "unevaluatedProperties", "unevaluatedItems"):
        if key in node and not isinstance(node[key], (dict, bool)):
            raise SchemaShapeError(f"{key} must be a schema")

    if "items" in node and not isinstance(node["items"], (dict, bool)):
        raise SchemaShapeError("items must be a schema")

    for key in ("allOf", "anyOf", "oneOf"):
        if key in node:
            val = node[key]
            if not isinstance(val, list) or not val or any(not isinstance(s, (dict, bool)) for s in val):
                raise SchemaShapeError(f"{key} must be a non-empty array of schemas")

    if "prefixItems" in node:
        val = node["prefixItems"]
        if not isinstance(val, list) or any(not isinstance(s, (dict, bool)) for s in val):
            raise SchemaShapeError("prefixItems must be an array of schemas")

    if "dependentRequired" in node:
        val = node["dependentRequired"]
        if not isinstance(val, dict) or any(
            not isinstance(k, str) or not isinstance(v, list) or any(not isinstance(r, str) for r in v)
            for k, v in val.items()
        ):
            raise SchemaShapeError("dependentRequired must map strings to string arrays")

    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref:
            raise SchemaShapeError("$ref must be a non-empty string")
        # 本地引用的可解析性由 validate_document_refs 校验；
        # 跨 Schema 引用（catalog://）在注册时建立依赖边。
    _validate_children(node, seen)


def _validate_children(node: dict, seen: frozenset[str]) -> None:
    for key in SCHEMA_MAP_KEYWORDS:
        if key in node:
            for sub in node[key].values():
                _validate_node(sub, seen)
    for key in ("not", "contains", "additionalProperties", "propertyNames", "additionalItems", "items", "unevaluatedProperties", "unevaluatedItems"):
        if key in node:
            _validate_node(node[key], seen)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if key in node:
            for sub in node[key]:
                _validate_node(sub, seen)


def validate_document_refs(doc: Any) -> None:
    """校验文档内所有本地 ``$ref`` 均可解析。"""

    def walk(node: Any, seen_pointers: frozenset[str]) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref.startswith("#"):
                pointer = ref[1:]
                try:
                    target = resolve_pointer(doc, pointer)
                except KeyError as exc:
                    raise SchemaShapeError(f"unresolved $ref: {ref}") from exc
                if pointer not in seen_pointers:
                    walk(target, seen_pointers | {pointer})
            # $ref 的兄弟关键字在本实现中不展开
            return
        for key in SCHEMA_MAP_KEYWORDS:
            if key in node:
                for sub in node[key].values():
                    walk(sub, seen_pointers)
        for key in ("not", "contains", "additionalProperties", "propertyNames", "additionalItems", "items", "unevaluatedProperties", "unevaluatedItems"):
            if key in node:
                walk(node[key], seen_pointers)
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            if key in node:
                for sub in node[key]:
                    walk(sub, seen_pointers)

    walk(doc, frozenset())


# ---------------------------------------------------------------------------
# Pointer / 路径工具
# ---------------------------------------------------------------------------

def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def resolve_pointer(doc: Any, pointer: str) -> Any:
    """按 JSON Pointer 解析；越界/缺键抛 KeyError。"""
    if pointer in ("", "#"):
        return doc
    if pointer.startswith("#"):
        pointer = pointer[1:]
    if not pointer.startswith("/"):
        raise KeyError(pointer)
    cur = doc
    for raw_token in pointer.split("/")[1:]:
        token = _unescape(raw_token)
        if isinstance(cur, list):
            try:
                idx = int(token)
            except ValueError as exc:
                raise KeyError(pointer) from exc
            if idx < 0 or idx >= len(cur):
                raise KeyError(pointer)
            cur = cur[idx]
        elif isinstance(cur, dict):
            if token not in cur:
                raise KeyError(pointer)
            cur = cur[token]
        else:
            raise KeyError(pointer)
    return cur


def join_path(base: str, token: str) -> str:
    return f"{base}/{_escape(token)}" if base else f"/{_escape(token)}"


def path_tokens(path: str) -> list[str]:
    if path in ("", "#"):
        return []
    if path.startswith("#"):
        path = path[1:]
    return [_unescape(t) for t in path.split("/")[1:]]


def is_prefix_path(prefix: str, path: str) -> bool:
    """prefix 是否为 path 的祖先或相等（根 '' 是一切路径前缀）。"""
    if prefix == "":
        return True
    return path == prefix or path.startswith(prefix + "/")


# ---------------------------------------------------------------------------
# 跨 Schema 引用
# ---------------------------------------------------------------------------

class ExternalRef:
    __slots__ = ("schema", "version", "pointer")

    def __init__(self, schema: str, version: str | None, pointer: str):
        self.schema = schema
        self.version = version
        self.pointer = pointer  # 目标 Schema 内的 JSON Pointer，根为 ""

    def as_key(self) -> tuple[str, str | None, str]:
        return (self.schema, self.version, self.pointer)

    def __repr__(self) -> str:
        return f"ExternalRef({self.schema!r}, {self.version!r}, {self.pointer!r})"


def parse_external_ref(ref: str) -> ExternalRef | None:
    """解析 ``catalog://name@version#/pointer``；非此外部形式返回 None。"""
    if not ref.startswith("catalog://"):
        return None
    body = ref[len("catalog://"):]
    if not body:
        raise SchemaShapeError(f"invalid external $ref: {ref}")
    fragment = ""
    if "#" in body:
        body, fragment = body.split("#", 1)
    # 根片段 "" 与 "/" 统一规范为根路径 ""
    if fragment in ("", "/"):
        pointer = ""
    elif fragment.startswith("/"):
        pointer = fragment
    else:
        raise SchemaShapeError(f"invalid $ref fragment: {ref}")
    version = None
    if "@" in body:
        name, version = body.split("@", 1)
        if not version:
            raise SchemaShapeError(f"invalid $ref version: {ref}")
    else:
        name = body
    if not name:
        raise SchemaShapeError(f"invalid $ref schema name: {ref}")
    return ExternalRef(name, version, pointer)


# ---------------------------------------------------------------------------
# 扁平化
# ---------------------------------------------------------------------------

class FieldConstraints:
    """某字段路径合并 allOf/$ref 后的确定性约束视图。"""

    __slots__ = (
        "path",
        "required",
        "types",
        "enum",
        "const",
        "has_default",
        "default",
        "format",
        "pattern",
        "numbers",
        "lengths",
        "title",
        "description",
        "comment",
        "external_refs",
    )

    def __init__(self, path: str):
        self.path = path
        self.required = False
        self.types: set[str] = set()
        self.enum: tuple[Any, ...] | None = None
        self.const: Any = None
        self.has_default = False
        self.default: Any = None
        self.format: str | None = None
        self.pattern: str | None = None
        self.numbers: dict[str, float] = {}
        self.lengths: dict[str, int] = {}
        self.title: str | None = None
        self.description: str | None = None
        self.comment: str | None = None
        self.external_refs: list[tuple[str, str | None, str]] = []


def canonical_json(doc: Any) -> str:
    """确定性 JSON 序列化（键排序、无多余空白）。"""
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def flatten(doc: Any) -> dict[str, FieldConstraints]:
    """把 Schema 扁平化为 ``路径 -> 约束``，路径按字典序排列。"""
    fields: dict[str, FieldConstraints] = {}

    def record(path: str) -> FieldConstraints:
        fc = fields.get(path)
        if fc is None:
            fc = FieldConstraints(path)
            fields[path] = fc
            if len(fields) > LIMITS["max_fields_per_schema"]:
                raise SchemaShapeError("schema exceeds max field count")
        return fc

    def visit(node: Any, path: str, required: bool, seen: frozenset[tuple[str, str]]) -> None:
        if node is False:
            # false Schema：该路径不接受任何值，记录空类型集合
            fc = record(path)
            fc.required |= required
            return
        if node is True:
            fc = record(path)
            fc.required |= required
            return
        if not isinstance(node, dict):
            return

        ref = node.get("$ref")
        if isinstance(ref, str):
            ext = parse_external_ref(ref)
            fc = record(path)
            fc.required |= required
            if ext is not None:
                key = ext.as_key()
                if key not in fc.external_refs:
                    fc.external_refs.append(key)
                return
            # 本地 $ref：展开到目标，带环检测
            pointer = ref[1:] if ref.startswith("#") else ref
            state = (pointer, path)
            if state in seen:
                return
            try:
                target = resolve_pointer(doc, pointer)
            except KeyError:
                raise SchemaShapeError(f"unresolved $ref: {ref}")
            visit(target, path, required, seen | {state})
            return

        fc = record(path)
        fc.required |= required

        if "type" in node:
            types = node["type"]
            if isinstance(types, str):
                types = [types]
            fc.types.update(types)

        if "enum" in node:
            vals = tuple(_hashable(v) for v in node["enum"])
            if fc.enum is None:
                fc.enum = vals
            else:
                # allOf 语义：枚举取交集
                common = [v for v in fc.enum if v in vals]
                fc.enum = tuple(common)

        if "const" in node:
            fc.const = _hashable(node["const"])

        if "default" in node:
            if not fc.has_default:
                fc.has_default = True
                fc.default = _hashable(node["default"])

        if isinstance(node.get("format"), str) and fc.format is None:
            fc.format = node["format"]
        if isinstance(node.get("pattern"), str) and fc.pattern is None:
            fc.pattern = node["pattern"]
        for key in NUMBER_CONSTRAINTS:
            if key in node and key not in fc.numbers and not isinstance(node[key], bool):
                fc.numbers[key] = node[key]
        for key in LENGTH_CONSTRAINTS:
            if key in node and key not in fc.lengths and not isinstance(node[key], bool):
                fc.lengths[key] = node[key]
        if isinstance(node.get("title"), str) and fc.title is None:
            fc.title = node["title"]
        if isinstance(node.get("description"), str) and fc.description is None:
            fc.description = node["description"]
        if isinstance(node.get("$comment"), str) and fc.comment is None:
            fc.comment = node["$comment"]

        required_names = set(node.get("required", []))
        # allOf 分支上声明的 required 与外层合并
        for sub in node.get("allOf", []):
            if isinstance(sub, dict):
                required_names.update(sub.get("required", []))

        props = node.get("properties")
        if isinstance(props, dict):
            for name in sorted(props):
                visit(props[name], join_path(path, name), name in required_names, seen)

        if isinstance(node.get("items"), (dict, bool)):
            visit(node["items"], path + "/[]", False, seen)

        prefix = node.get("prefixItems")
        if isinstance(prefix, list):
            for idx, sub in sorted(enumerate(prefix), key=lambda x: x[0]):
                if isinstance(sub, (dict, bool)):
                    visit(sub, join_path(path, str(idx)), False, seen)

        for sub in node.get("allOf", []):
            visit(sub, path, required, seen)
        # anyOf/oneOf/not 表示可选分支，字段不确定存在，不展开为字段路径

    visit(doc, "", False, frozenset())
    return dict(sorted(fields.items(), key=lambda kv: kv[0]))


def _hashable(value: Any) -> Any:
    """把 JSON 值转成可比较/可哈希的规范形式。"""
    if isinstance(value, dict):
        return ("__obj__", tuple(sorted((k, _hashable(v)) for k, v in value.items())))
    if isinstance(value, list):
        return ("__arr__", tuple(_hashable(v) for v in value))
    return value


def _from_hashable(value: Any) -> Any:
    """把 :func:`_hashable` 的内部形式还原为原生 JSON 值。"""
    if isinstance(value, tuple) and len(value) == 2 and value[0] in ("__obj__", "__arr__"):
        if value[0] == "__obj__":
            return {k: _from_hashable(v) for k, v in value[1]}
        return [_from_hashable(v) for v in value[1]]
    return value


def summarize(fc: FieldConstraints) -> dict[str, Any]:
    """生成字段定义摘要（确定性键序）。"""
    summary: dict[str, Any] = {}
    summary["required"] = fc.required
    if fc.types:
        summary["type"] = sorted(fc.types)
    if fc.enum is not None:
        summary["enum"] = [_from_hashable(v) for v in fc.enum]
    if fc.const is not None:
        summary["const"] = _from_hashable(fc.const)
    if fc.has_default:
        summary["default"] = _from_hashable(fc.default)
    if fc.format is not None:
        summary["format"] = fc.format
    if fc.pattern is not None:
        summary["pattern"] = fc.pattern
    for key in NUMBER_CONSTRAINTS:
        if key in fc.numbers:
            summary[key] = fc.numbers[key]
    for key in LENGTH_CONSTRAINTS:
        if key in fc.lengths:
            summary[key] = fc.lengths[key]
    if fc.title is not None:
        summary["title"] = fc.title
    if fc.description is not None:
        summary["description"] = fc.description
    if fc.comment is not None:
        summary["$comment"] = fc.comment
    if fc.external_refs:
        summary["$refs"] = [
            f"catalog://{name}@{version}#{pointer}" if version else f"catalog://{name}#{pointer}"
            for name, version, pointer in sorted(fc.external_refs)
        ]
    return dict(sorted(summary.items()))


def meta_signature(fc: FieldConstraints) -> tuple[Any, ...]:
    return (fc.title, fc.description, fc.comment)


def constraint_signature(fc: FieldConstraints) -> tuple[Any, ...]:
    """除元数据外的完整约束指纹。"""
    return (
        fc.required,
        tuple(sorted(fc.types)),
        fc.enum,
        fc.const,
        fc.has_default,
        fc.default if fc.has_default else None,
        fc.format,
        fc.pattern,
        tuple(sorted(fc.numbers.items())),
        tuple(sorted(fc.lengths.items())),
        tuple(sorted(fc.external_refs)),
    )


def prepare(raw: str | bytes | dict) -> tuple[Any, dict[str, FieldConstraints], str]:
    """解析 + 校验 + 扁平化，返回 (文档, 字段表, 规范 JSON)。"""
    doc = parse_json(raw)
    validate(doc)
    validate_document_refs(doc)
    fields = flatten(doc)
    return doc, fields, canonical_json(doc)


def schema_error_for_registration(exc: SchemaShapeError) -> CatalogError:
    return CatalogError(str(exc), code=ErrorCode.SCHEMA_INVALID)
