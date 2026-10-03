"""字段路径工具。

字段路径采用 JSON Pointer（RFC 6901）表示，例如 ``/properties/name``、
``/properties/a~1b``。根对象本身用空串 ``""`` 表示。
"""

from __future__ import annotations

from typing import Any


def escape(token: str) -> str:
    """转义单个路径段。"""
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    """反转义单个路径段。"""
    return token.replace("~1", "/").replace("~0", "~")


def parse(path: str) -> tuple[str, ...]:
    """把 JSON Pointer 解析为路径段元组；根路径返回空元组。"""
    if path == "":
        return ()
    if not path.startswith("/"):
        raise ValueError(f"非法 JSON Pointer: {path!r}")
    return tuple(unescape(seg) for seg in path[1:].split("/"))


def format(segments: tuple[str, ...] | list[str]) -> str:
    """把路径段组装为 JSON Pointer。"""
    return "".join("/" + escape(str(seg)) for seg in segments)


def child(path: str, segment: str) -> str:
    """返回某路径下的子路径。"""
    return (path + "/" + escape(segment)) if path else "/" + escape(segment)


def parent(path: str) -> str | None:
    """返回父路径；根路径返回 ``None``。"""
    segs = parse(path)
    if not segs:
        return None
    return format(segs[:-1])


def is_under(prefix: str, path: str) -> bool:
    """``path`` 是否位于 ``prefix`` 子树内（含自身）。"""
    p, c = parse(prefix), parse(path)
    return len(c) >= len(p) and c[: len(p)] == p


def resolve(doc: Any, path: str) -> Any:
    """在文档中按 JSON Pointer 取值，缺段/越界/类型不符时抛 KeyError。"""
    cur: Any = doc
    for seg in parse(path):
        if isinstance(cur, dict):
            if seg not in cur:
                raise KeyError(path)
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                idx = int(seg)
            except ValueError as exc:
                raise KeyError(path) from exc
            if not 0 <= idx < len(cur):
                raise KeyError(path)
            cur = cur[idx]
        else:
            raise KeyError(path)
    return cur


def normalize_field_pointer(path: str) -> str:
    """把 Schema 文档指针 *无上下文地* 规整为逻辑字段路径。

    注意：纯文本规整无法区分「名为 ``items`` 的属性」与 ``items`` 关键字，
    资产/引用路径应优先使用逻辑路径，或用 :func:`resolve_logical` 结合
    文档解析。
    """
    segs = list(parse(path))
    out: list[str] = []
    for i, seg in enumerate(segs):
        if seg in ("properties", "prefixItems"):
            continue
        if seg == "additionalProperties":
            out.append("*")
        elif seg == "items":
            # 元组形式 items/0 展开为数组下标；单 Schema items 展开为 -。
            if i + 1 < len(segs) and segs[i + 1].isdigit():
                continue
            out.append("-")
        else:
            out.append(seg)
    return format(out)


def resolve_logical(document: Any, raw_path: str) -> str | None:
    """结合文档结构，把文档指针解析为逻辑字段路径；不可解析返回 ``None``。

    遍历规则与字段展开一致：``properties`` 段省略（其后必为属性名，即使
    名字恰好叫 ``items``）；单 Schema ``items`` 段映射为 ``-``；
    ``additionalProperties`` 映射为 ``*``；``$defs``/``definitions`` 保留。
    """
    segs = parse(raw_path)
    node: Any = document
    logical: list[str] = []
    i = 0
    while i < len(segs):
        seg = segs[i]
        if isinstance(node, dict):
            props = node.get("properties")
            if seg == "properties" and isinstance(props, dict):
                # 容器关键字与其后的属性名在同一步消费，避免属性名与
                # Schema 关键字（如名为 items 的属性）混淆。
                if i + 1 >= len(segs):
                    return None
                name = segs[i + 1]
                if name not in props:
                    return None
                node = props[name]
                logical.append(name)
                i += 2
                continue
            pats = node.get("patternProperties")
            if seg == "patternProperties" and isinstance(pats, dict):
                if i + 1 >= len(segs):
                    return None
                name = segs[i + 1]
                if name not in pats:
                    return None
                node = pats[name]
                logical.append(escape(name))
                i += 2
                continue
            ap = node.get("additionalProperties")
            if seg == "additionalProperties" and isinstance(ap, (dict, bool)):
                node = ap
                logical.append("*")
                i += 1
                continue
            items = node.get("items")
            if seg == "items" and isinstance(items, (dict, bool)):
                node = items
                logical.append("-")
                i += 1
                continue
            pre = node.get("prefixItems")
            if seg == "prefixItems" and isinstance(pre, list):
                if i + 1 >= len(segs):
                    return None
                try:
                    idx = int(segs[i + 1])
                except ValueError:
                    return None
                if not 0 <= idx < len(pre):
                    return None
                node = pre[idx]
                logical.append(str(idx))
                i += 2
                continue
            if seg in ("$defs", "definitions") and isinstance(node.get(seg), dict):
                if i + 1 >= len(segs):
                    return None
                name = segs[i + 1]
                if name not in node[seg]:
                    return None
                node = node[seg][name]
                logical.extend((seg, name))
                i += 2
                continue
            if seg not in node:
                return None
            node = node[seg]
            logical.append(seg)
            i += 1
        elif isinstance(node, list):
            try:
                idx = int(seg)
            except ValueError:
                return None
            if not 0 <= idx < len(node):
                return None
            node = node[idx]
            logical.append(seg)
            i += 1
        else:
            return None
    return format(logical)
