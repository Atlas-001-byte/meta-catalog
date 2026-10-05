"""跨 Schema 引用完整性审计（只读）。

审计每个已注册 Schema 版本中声明的跨 Schema ``$ref``（形如
``Name@version#pointer``），文档内 ``#/`` 引用不在审计范围内。

引用状态（``status``）：

  * ``resolved``        目标版本存在，片段对应的逻辑字段可达；
  * ``missing_schema``  目标 Schema 版本未注册（允许前向引用，注册期不报错）；
  * ``invalid_pointer`` 目标版本存在，但片段不能解释为字段指针（容器关键字
                        后缺字段名、数组下标越界/非数字、穿过非 Schema 取值
                        等结构性非法情形）；
  * ``missing_field``   片段能解释为逻辑字段路径，但该字段在目标中不可达：
                        属性/定义名缺失，或既不在目标字段表中、也无法沿跨
                        Schema ``$ref`` 边跳转到达。可达性口径与
                        :mod:`meta_catalog.impact` 一致，引用环按实际引用
                        位点访问一次后终止。

字段路径口径同样沿用影响分析：根路径为空串、数组元素为 ``-``、
``additionalProperties`` 为 ``*``。相同输入始终给出相同的统计、排序与
结论；审计不改动注册内容、不写入检索索引。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import pointer as ptr, schema_fields as sf
from .errors import NotFoundError
from .registry import RefEdge, Registry, parse_external_ref, extract_ref_edges

RESOLVED = "resolved"
MISSING_SCHEMA = "missing_schema"
MISSING_FIELD = "missing_field"
INVALID_POINTER = "invalid_pointer"

# 结构解释结果：
#   ("field", logical_path)   片段完整落在某个字段节点上
#   ("missing", logical_path) 片段意图指向某字段，但字段在文档中缺失
#   ("edge", logical_path)    停在外部 $ref 节点上，剩余片段需跨边继续
#   ("invalid", None)         片段结构性非法，不构成字段指针
_FIELD = "field"
_MISSING = "missing"
_EDGE = "edge"
_INVALID = "invalid"


@dataclass(frozen=True)
class _Edge:
    """逻辑化后的跨 Schema 引用边（两端均按当前文档重新解析）。"""

    src_schema: str
    src_version: str
    src_path: str
    dst_schema: str
    dst_version: str
    dst_path: str
    fragment: str


@dataclass(frozen=True)
class _Finding:
    source_schema: str
    source_version: str
    source_path: str
    target_schema: str
    target_version: str
    target_path: str
    target_fragment: str
    status: str


def _select_sources(
    registry: Registry, name: str | None, version: str | None
) -> list[tuple[str, str]]:
    """按参数语义选出受审的 ``(名称, 版本)``，保持注册顺序；无匹配抛 NotFound。"""
    pairs = registry.registered_pairs()
    if name is not None and version is not None:
        if not registry.has_schema(name, version):
            raise NotFoundError(
                f"Schema {name}@{version} 不存在",
                details={"schema": name, "version": version},
            )
        return [(name, version)]
    if name is not None:
        # name 不存在时 list_versions 抛 NotFoundError。
        return [(name, v) for v in registry.list_versions(name)]
    if version is not None:
        matched = [(n, v) for n, v in pairs if v == version]
        if not matched:
            raise NotFoundError(
                f"版本 {version} 下没有已注册的 Schema",
                details={"version": version},
            )
        return matched
    return list(pairs)


def _logical_path(document: Any, raw_path: str) -> str:
    """文档指针 -> 逻辑字段路径；无法结构化解析时退回无上下文规整。"""
    return ptr.resolve_logical(document, raw_path) or ptr.normalize_field_pointer(
        raw_path
    )


def _build_edges(registry: Registry) -> dict[tuple[str, str], tuple[_Edge, ...]]:
    """从不可变注册文档重新提取并逻辑化全部跨 Schema 引用边。

    前向引用在注册期只能按文本规整目标路径；审计时目标通常已注册，这里
    结合两端当前文档重新解析，使可达性判定与引用书写顺序无关。
    """
    out: dict[tuple[str, str], list[_Edge]] = {}
    for src_name, src_version in registry.registered_pairs():
        document = registry.get_schema(src_name, src_version).document
        edges: list[_Edge] = []
        raw: RefEdge
        for raw in extract_ref_edges(src_name, src_version, document):
            if registry.has_schema(raw.dst_schema, raw.dst_version):
                dst_doc = registry.get_schema(
                    raw.dst_schema, raw.dst_version
                ).document
                dst_path = _logical_path(dst_doc, raw.dst_path)
            else:
                dst_path = ptr.normalize_field_pointer(raw.dst_path)
            edges.append(
                _Edge(
                    src_name,
                    src_version,
                    _logical_path(document, raw.src_path),
                    raw.dst_schema,
                    raw.dst_version,
                    dst_path,
                    raw.dst_path,
                )
            )
        out[(src_name, src_version)] = tuple(edges)
    return out


def _external_ref(node: Any) -> tuple[str, str, str] | None:
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        return parse_external_ref(node["$ref"])
    return None


def _interpret(document: Any, fragment: str) -> tuple[str, str | None]:
    """把原始片段结合目标文档结构解释为逻辑字段路径。

    规则与 :func:`meta_catalog.pointer.resolve_logical` 的字段展开口径一致，
    但对「字段缺失」与「片段结构性非法」加以区分，并在停于外部 ``$ref``
    节点时提示调用方沿跨 Schema 边继续解析。
    """
    try:
        segs = ptr.parse(fragment)
    except ValueError:
        return _INVALID, None

    node: Any = document
    logical: list[str] = []
    i = 0
    while i < len(segs):
        seg = segs[i]

        if isinstance(node, bool) or not isinstance(node, (dict, list)):
            # 穿过叶子 Schema/标量继续取段：字段不可达（非结构性非法）。
            suffix = ptr.normalize_field_pointer(ptr.format(segs[i:]))
            return _MISSING, ptr.format(list(logical) + list(ptr.parse(suffix)))

        if isinstance(node, list):
            try:
                idx = int(seg)
            except ValueError:
                return _INVALID, None
            if not 0 <= idx < len(node):
                return _INVALID, None
            node = node[idx]
            logical.append(seg)
            i += 1
            continue

        # 停在外部 $ref 节点上、仍有后续段：交由边跳转解析。
        if _external_ref(node) is not None:
            suffix = ptr.normalize_field_pointer(ptr.format(segs[i:]))
            return _EDGE, ptr.format(list(logical) + list(ptr.parse(suffix)))

        props = node.get("properties")
        if seg == "properties":
            if i + 1 >= len(segs) or not isinstance(props, dict):
                return _INVALID, None
            name = segs[i + 1]
            if name not in props:
                return _MISSING, ptr.format(logical + [name])
            node = props[name]
            logical.append(name)
            i += 2
            continue

        pats = node.get("patternProperties")
        if seg == "patternProperties":
            if i + 1 >= len(segs) or not isinstance(pats, dict):
                return _INVALID, None
            name = segs[i + 1]
            if name not in pats:
                return _MISSING, ptr.format(logical + [ptr.escape(name)])
            node = pats[name]
            logical.append(ptr.escape(name))
            i += 2
            continue

        ap = node.get("additionalProperties")
        if seg == "additionalProperties":
            if isinstance(ap, dict):
                node = ap
                logical.append("*")
                i += 1
                continue
            return _INVALID, None

        items = node.get("items")
        if seg == "items":
            if isinstance(items, dict):
                node = items
                logical.append("-")
                i += 1
                continue
            if isinstance(items, list):
                if i + 1 >= len(segs):
                    return _INVALID, None
                raw_idx = segs[i + 1]
                try:
                    idx = int(raw_idx)
                except ValueError:
                    return _INVALID, None
                if not 0 <= idx < len(items):
                    return _INVALID, None
                node = items[idx]
                logical.append(raw_idx)
                i += 2
                continue
            return _INVALID, None

        pre = node.get("prefixItems")
        if seg == "prefixItems":
            if i + 1 >= len(segs) or not isinstance(pre, list):
                return _INVALID, None
            raw_idx = segs[i + 1]
            try:
                idx = int(raw_idx)
            except ValueError:
                return _INVALID, None
            if not 0 <= idx < len(pre):
                return _INVALID, None
            node = pre[idx]
            logical.append(raw_idx)
            i += 2
            continue

        if seg in ("$defs", "definitions"):
            defs = node.get(seg)
            if i + 1 >= len(segs) or not isinstance(defs, dict):
                return _INVALID, None
            name = segs[i + 1]
            if name not in defs:
                return _MISSING, ptr.format(logical + [seg, name])
            node = defs[name]
            logical.extend((seg, name))
            i += 2
            continue

        # 其余段：键存在则继续下沉；落在非 Schema 取值上为非法。
        if seg not in node:
            # 兼容直接书写逻辑路径（/name）与未知名：先按候选字段处理，
            # 最终由字段表/跨边可达性裁决。
            return _MISSING, ptr.format(logical + [seg] + list(segs[i + 1 :]))
        node = node[seg]
        logical.append(seg)
        i += 1

    # 片段耗尽：落点必须是一个字段 Schema（dict/bool）；标量/容器关键字非法。
    if isinstance(node, (dict, bool)):
        return _FIELD, ptr.format(logical)
    return _INVALID, None


def _field_exists(registry: Registry, schema: str, version: str, path: str) -> bool:
    document = registry.get_schema(schema, version).document
    return path in sf.expand(document)


def _reachable(
    registry: Registry,
    edges: dict[tuple[str, str], tuple[_Edge, ...]],
    schema: str,
    version: str,
    path: str,
) -> bool:
    """逻辑字段路径是否可达：自身为字段，或沿覆盖当前路径前缀的出边跳转可达。

    出边匹配与后缀对齐和 :func:`meta_catalog.impact.forward_reach` 同口径
    （全部前缀匹配出边都会尝试，取最长/任一可达即可）。每条解析分支上同一
    目标 ``(Schema, 版本)`` 只进入一次，因此引用环按实际引用位点判断一次后
    终止；分支数与版本数均有限，遍历必然结束。
    """
    positive: set[tuple[str, str, str]] = set()

    def search(
        cur_schema: str,
        cur_version: str,
        cur_path: str,
        visited_versions: frozenset[tuple[str, str]],
    ) -> bool:
        if (cur_schema, cur_version, cur_path) in positive:
            return True
        if _field_exists(registry, cur_schema, cur_version, cur_path):
            positive.add((cur_schema, cur_version, cur_path))
            return True

        cur_segs = ptr.parse(cur_path)
        for edge in edges.get((cur_schema, cur_version), ()):
            edge_segs = ptr.parse(edge.src_path)
            if not (
                len(edge_segs) <= len(cur_segs)
                and cur_segs[: len(edge_segs)] == edge_segs
            ):
                continue
            dst_key = (edge.dst_schema, edge.dst_version)
            if dst_key in visited_versions or not registry.has_schema(*dst_key):
                continue
            suffix = cur_segs[len(edge_segs) :]
            mapped = ptr.format(list(ptr.parse(edge.dst_path)) + list(suffix))
            if search(
                edge.dst_schema,
                edge.dst_version,
                mapped,
                visited_versions | {dst_key},
            ):
                return True
        return False

    if not registry.has_schema(schema, version):
        return False
    return search(schema, version, path, frozenset({(schema, version)}))


def _classify(
    registry: Registry,
    edges: dict[tuple[str, str], tuple[_Edge, ...]],
    edge: _Edge,
) -> str:
    """判定单条引用边状态。"""
    if not registry.has_schema(edge.dst_schema, edge.dst_version):
        return MISSING_SCHEMA

    document = registry.get_schema(edge.dst_schema, edge.dst_version).document
    kind, logical = _interpret(document, edge.fragment)
    if kind == _INVALID:
        return INVALID_POINTER
    # field / missing / edge 三种解释统一交给跨边可达性裁决。
    if _reachable(
        registry, edges, edge.dst_schema, edge.dst_version, logical or edge.dst_path
    ):
        return RESOLVED
    return MISSING_FIELD


def _message(f: _Finding) -> str:
    where = f"{f.target_schema}@{f.target_version}{f.target_fragment}"
    if f.status == MISSING_SCHEMA:
        return f"跨 Schema 引用的目标 {f.target_schema}@{f.target_version} 未注册"
    if f.status == INVALID_POINTER:
        return f"跨 Schema 引用 {where} 的片段不是目标文档中的合法字段指针"
    return f"跨 Schema 引用 {where} 的字段路径 {f.target_path} 在目标版本中不可达"


def check_references(
    registry: Registry, name: str | None = None, version: str | None = None
) -> dict[str, Any]:
    """审计受审 Schema 版本中的全部跨 Schema ``$ref``。"""
    sources = _select_sources(registry, name, version)
    source_set = set(sources)
    edges = _build_edges(registry)

    findings: list[_Finding] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    unresolved_sources: set[tuple[str, str]] = set()

    for src_name, src_version in sources:
        for edge in edges.get((src_name, src_version), ()):
            status = _classify(registry, edges, edge)
            identity = (
                src_name,
                src_version,
                edge.src_path,
                edge.dst_schema,
                edge.dst_version,
                edge.dst_path,
            )
            if identity in seen:
                continue
            seen.add(identity)
            if status != RESOLVED:
                unresolved_sources.add((src_name, src_version))
            findings.append(
                _Finding(
                    src_name,
                    src_version,
                    edge.src_path,
                    edge.dst_schema,
                    edge.dst_version,
                    edge.dst_path,
                    edge.fragment,
                    status,
                )
            )

    total = len(sources)
    resolved = total - len(unresolved_sources & source_set)

    # 逻辑化后按定位六元组稳定排序，去重与书写位置无关。
    findings.sort(
        key=lambda f: (
            f.source_schema,
            f.source_version,
            tuple(ptr.parse(f.source_path)),
            f.source_path,
            f.target_schema,
            f.target_version,
            tuple(ptr.parse(f.target_path)),
            f.target_path,
        )
    )

    references = [
        {
            "source_schema": f.source_schema,
            "source_version": f.source_version,
            "source_path": f.source_path,
            "target_schema": f.target_schema,
            "target_version": f.target_version,
            "target_path": f.target_path,
            "status": f.status,
        }
        for f in findings
    ]

    issues = [
        {
            "source_schema": f.source_schema,
            "source_version": f.source_version,
            "source_path": f.source_path,
            "target_schema": f.target_schema,
            "target_version": f.target_version,
            "target_path": f.target_path,
            "reason": f.status,
            "message": _message(f),
        }
        for f in findings
        if f.status != RESOLVED
    ]

    return {
        "checked": total,
        "total": total,
        "resolved": resolved,
        "issues": issues,
        "references": references,
    }
