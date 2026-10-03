"""把 JSON Schema 展开为扁平字段表，并给出字段特征。

只展开业务字段所在的容器：根节点、``properties``、``$defs``/``definitions``
里的对象、以及 ``items``/``prefixItems``/``additionalProperties`` 等容器层。
``allOf``/``anyOf``/``oneOf`` 等组合子节点不参与字段路径比较（避免同一逻辑
字段出现多重路径），但其 ``required`` 约束会被合并到所在对象节点。
"""

from __future__ import annotations

import json
from typing import Any

from . import pointer as ptr

# 纯注解关键字：只调整这些时归类为 metadata。
ANNOTATION_KEYS = ("title", "description", "$comment")

# 结构关键字（参与兼容判定）。顺序同时决定摘要输出顺序，保证确定性。
STRUCTURAL_KEYS = (
    "type",
    "enum",
    "const",
    "required",
    "default",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
)

# 嵌套容器关键字：不在同路径字段比较范围内，由子字段条目负责。
CONTAINER_KEYS = frozenset(
    {
        "properties",
        "patternProperties",
        "additionalProperties",
        "items",
        "prefixItems",
        "contains",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "$defs",
        "definitions",
        "$ref",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
    }
)

# 放宽/收窄判定关心的数值边界关键字。
_BOUND_KEYS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
    "pattern",
    "format",
    "multipleOf",
    "uniqueItems",
)


class Field:
    """一个展开后的字段节点。

    :ivar path: 字段的 JSON Pointer 路径。
    :ivar schema: 该字段的生效 Schema 片段（已合并 allOf/required）。
    :ivar required_paths: 该对象节点直接要求必填的子字段路径集合。
    """

    __slots__ = ("path", "schema", "required_paths")

    def __init__(self, path: str, schema: dict, required_paths: frozenset[str]):
        self.path = path
        self.schema = schema
        self.required_paths = required_paths


def _effective(node: dict) -> dict:
    """返回字段的生效定义：合并 allOf 的结构关键字与 required。

    不改变原节点；不递归 anyOf/oneOf（分支为可选，不构成确定约束）。
    """
    merged: dict[str, Any] = {k: v for k, v in node.items() if k != "allOf"}
    required: list[Any] = list(node.get("required", []))
    for sub in node.get("allOf", []) or []:
        if not isinstance(sub, dict):
            continue
        for k, v in sub.items():
            if k == "required":
                required.extend(v)
            elif k == "allOf":
                continue
            elif k not in merged:
                merged[k] = v
    if required:
        # 去重但保持稳定顺序。
        seen: set[str] = set()
        uniq = []
        for r in required:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        merged["required"] = uniq
    return merged


def expand(document: Any) -> dict[str, Field]:
    """把文档展开为 ``{path: Field}``。"""
    fields: dict[str, Field] = {}
    if isinstance(document, bool):
        fields[""] = Field("", {} if document else {}, frozenset())
        return fields

    def add(path: str, raw: Any) -> None:
        if isinstance(raw, bool):
            raw = {} if raw else {"not": {}}
        eff = _effective(raw)
        req = frozenset(
            ptr.child(path, name)
            for name in eff.get("required", [])
            if isinstance(name, str)
        )
        fields[path] = Field(path, eff, req)

    def walk(node: Any, path: str, seen: frozenset[int]) -> None:
        if isinstance(node, bool):
            add(path, node)
            return
        if not isinstance(node, dict):
            return
        if id(node) in seen:
            return
        seen = seen | {id(node)}

        add(path, node)
        eff = fields[path].schema

        for name in sorted(eff.get("properties", {}).keys()):
            walk(eff["properties"][name], ptr.child(path, name), seen)

        for def_kw in ("$defs", "definitions"):
            defs = eff.get(def_kw)
            if isinstance(defs, dict):
                for dname in sorted(defs.keys()):
                    sub = defs[dname]
                    if isinstance(sub, dict) or isinstance(sub, bool):
                        walk(sub, ptr.child(ptr.child(path, def_kw), dname), seen)

        items = eff.get("items")
        if isinstance(items, dict):
            walk(items, ptr.child(path, "-"), seen)
        elif isinstance(items, list):
            for i, sub in enumerate(items):
                if isinstance(sub, dict):
                    walk(sub, path + "/" + str(i), seen)
        for i, sub in enumerate(eff.get("prefixItems", []) or []):
            if isinstance(sub, dict):
                walk(sub, path + "/" + str(i), seen)
        ap = eff.get("additionalProperties")
        if isinstance(ap, dict):
            walk(ap, path + "/*", seen)
        pp = eff.get("patternProperties")
        if isinstance(pp, dict):
            for name in sorted(pp.keys()):
                sub = pp[name]
                if isinstance(sub, dict):
                    walk(sub, path + "/" + ptr.escape(name), seen)

    walk(document, "", frozenset())
    return fields


def summary(field_schema: dict) -> dict[str, Any]:
    """字段定义摘要：确定性的、可 JSON 序列化的结构关键字快照。"""
    out: dict[str, Any] = {}
    for key in STRUCTURAL_KEYS:
        if key in field_schema:
            out[key] = _canonical(field_schema[key])
    return out


def _canonical(value: Any) -> Any:
    """把取值转为可比较、可 JSON 序列化的规范形式。"""
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 特征提取与兼容判定
# ---------------------------------------------------------------------------


def _types(schema: dict) -> frozenset[str]:
    t = schema.get("type")
    if t is None:
        return frozenset()
    return frozenset(t if isinstance(t, list) else [t])


def _enum(schema: dict):
    if "enum" in schema:
        return tuple(json.dumps(v, sort_keys=True, ensure_ascii=False) for v in schema["enum"])
    if "const" in schema:
        return (json.dumps(schema["const"], sort_keys=True, ensure_ascii=False),)
    return None


def _bounds(schema: dict) -> dict[str, Any]:
    return {k: _canonical(schema[k]) for k in _BOUND_KEYS if k in schema}


def annotation_signature(schema: dict) -> dict[str, Any]:
    return {k: schema[k] for k in ANNOTATION_KEYS if k in schema}


# 兼容内部旧命名。
_annotation_signature = annotation_signature


def structural_signature(schema: dict) -> dict[str, Any]:
    """同路径字段的结构特征（排除注解、嵌套容器与 required）。

    嵌套容器由各自展开出的子字段条目负责；required 变化由编排层按
    子字段生成 required_added/required_removed 条目。
    """
    return {
        k: _canonical(v)
        for k, v in schema.items()
        if k not in ANNOTATION_KEYS and k not in CONTAINER_KEYS and k != "required"
    }


def classify_pair(old: dict, new: dict) -> str:
    """比较同一路径字段的新旧 *结构* 定义，返回兼容结论。

    仅当结构签名确实不同时调用（注解-only 变化由编排层判为 metadata）。

    约定：
      * 放宽枚举、integer→number、增加默认值 → compatible；
      * 收窄类型/枚举、允许原本不接受的 null、新增约束 → breaking；
      * 同时有收有放按 breaking。
    """
    if structural_signature(old) == structural_signature(new):
        return "metadata"

    verdicts: list[str] = []

    # 1) 类型：integer -> number 放宽；出现新增的 null 为破坏性；其余集合变化收紧/扩展。
    ot, nt = _types(old), _types(new)
    if nt != ot:
        if ot == frozenset({"integer"}) and nt == frozenset({"number"}):
            verdicts.append("compatible")
        else:
            added = nt - ot
            if "null" in added:
                verdicts.append("breaking")
            elif ot and not nt:
                verdicts.append("compatible")  # 取消类型约束 = 放宽
            elif ot and nt and nt > ot:
                verdicts.append("compatible")  # 类型集合扩大
            elif not ot:
                # 旧未约束类型、新给出具体类型：视为收窄。
                verdicts.append("breaking")
            else:
                verdicts.append("breaking")

    # 2) 枚举/const：取值集合扩大为放宽，缩小为收窄。
    oe, ne = _enum(old), _enum(new)
    if oe != ne:
        if oe is None and ne is None:
            pass
        elif oe is not None and ne is not None:
            oset, nset = frozenset(oe), frozenset(ne)
            verdicts.append("compatible" if oset < nset else "breaking")
        elif ne is None:
            verdicts.append("compatible")  # 取消枚举 = 放宽
        else:
            verdicts.append("breaking")  # 新增强枚举 = 收窄

    # 3) 数值/长度等边界：逐项比较。
    ob, nb = _bounds(old), _bounds(new)
    if ob != nb:
        verdicts.extend(_classify_bounds(ob, nb))

    # 4) default：增加默认值明确属于兼容；移除或改变默认值会让依赖缺省
    #    行为的数据发生变化，保守归为破坏性。
    if "default" in new and "default" not in old:
        verdicts.append("compatible")
    elif old.get("default") != new.get("default"):
        verdicts.append("breaking")

    # 5) 其余结构键（未知/新增约束键）：出现即为新增约束 → breaking；
    #    消失为放宽 → compatible。
    known = set(STRUCTURAL_KEYS) | set(ANNOTATION_KEYS) | {"default"}

    def is_extra(k: str) -> bool:
        return (
            k not in known
            and not k.startswith("$")
            and k not in CONTAINER_KEYS
        )

    extra_o = {k for k in old if is_extra(k)}
    extra_n = {k for k in new if is_extra(k)}
    if extra_n - extra_o:
        verdicts.append("breaking")
    if extra_o - extra_n:
        verdicts.append("compatible")

    if "breaking" in verdicts:
        return "breaking"
    if "compatible" in verdicts:
        return "compatible"
    return "metadata"


def _classify_bounds(ob: dict, nb: dict) -> list[str]:
    """对数值边界做放宽/收窄判定。

    lower 类关键字（minimum 等）减小为放宽；upper 类增大为放宽；
    pattern/format/multipleOf/uniqueItems 出现或收紧为 breaking。
    """
    out: list[str] = []
    lower = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
    upper = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")

    for key in sorted(set(ob) | set(nb)):
        ov, nv = ob.get(key), nb.get(key)
        if ov == nv:
            continue
        if nv is None:
            out.append("compatible")  # 移除边界 = 放宽
            continue
        if ov is None:
            out.append("breaking")  # 新增边界 = 收窄
            continue
        if key in lower:
            out.append("compatible" if nv < ov else "breaking")
        elif key in upper:
            out.append("compatible" if nv > ov else "breaking")
        else:
            # pattern / format / multipleOf / uniqueItems：改变既有约束 = 收窄。
            out.append("breaking")
    return out
