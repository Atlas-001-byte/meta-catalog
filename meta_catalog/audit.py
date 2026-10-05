"""跨 Schema 引用完整性审计。

只读检查已注册 Schema 版本中的跨 Schema ``$ref``（``Name@version#pointer``）：
目标 Schema 是否已注册、片段是否为合法 JSON Pointer、目标字段是否可达。
文档内 ``#/`` 引用不参与检查；审计不改动任何注册内容、版本关系、资产依赖
与检索索引，相同输入始终返回相同结果。

字段路径约定与影响分析一致：根路径为空串，数组元素为 ``-``，
``additionalProperties`` 为 ``*``。字段可达性沿跨 Schema 引用边跳转判断，
引用环在每个实际引用位点按栈去重、判断一次即终止。
"""

from __future__ import annotations

import re
from typing import Any

from . import pointer as ptr
from .errors import NotFoundError
from .registry import Registry, parse_external_ref

# RFC 6901：`~` 只允许转义为 ~0 / ~1。
_BAD_TILDE_RE = re.compile(r"~(?![01])")


def _extract_external_refs(document: Any) -> list[tuple[str, str, str, str]]:
    """提取文档内全部跨 Schema 引用位点。

    返回 ``(源文档指针, 目标名, 目标版本, 原始片段)``；文档内 ``#/`` 引用与
    非外部形式的 ``$ref`` 不纳入。
    """
    refs: list[tuple[str, str, str, str]] = []

    def walk(node: Any, path: str, seen: frozenset[int]) -> None:
        if isinstance(node, bool) or not isinstance(node, (dict, list)):
            return
        if id(node) in seen:
            return
        seen = seen | {id(node)}
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                parsed = parse_external_ref(ref)
                if parsed is not None:
                    dst_name, dst_version, fragment = parsed
                    refs.append((path, dst_name, dst_version, fragment))
            for k in sorted(node.keys()):
                walk(node[k], path + "/" + ptr.escape(k) if path else "/" + ptr.escape(k), seen)
        else:
            for i, v in enumerate(node):
                walk(v, f"{path}/{i}" if path else f"/{i}", seen)

    walk(document, "", frozenset())
    return refs


def _fragment_valid(fragment: str) -> bool:
    """片段是否为合法 JSON Pointer（空串表示根，否则须以 ``/`` 开头）。"""
    if fragment == "":
        return True
    if not fragment.startswith("/"):
        return False
    return _BAD_TILDE_RE.search(fragment) is None


def _normalize_fragment(fragment: str) -> str:
    """把引用片段规整为逻辑字段路径（无上下文，仅用于报告展示）。"""
    if fragment and not fragment.startswith("/"):
        fragment = "/" + fragment
    try:
        return ptr.normalize_field_pointer(fragment)
    except ValueError:
        return fragment


def _audit_ref(
    registry: Registry,
    src_document: Any,
    src_raw_path: str,
    dst_name: str,
    dst_version: str,
    fragment: str,
) -> dict[str, str]:
    """检查单个跨 Schema 引用位点，返回稳定排序用的定位字段与状态。"""
    src_logical = ptr.resolve_logical(src_document, src_raw_path)
    if src_logical is None:
        src_logical = ptr.normalize_field_pointer(src_raw_path)

    entry = {
        "source_path": src_logical,
        "target_schema": dst_name,
        "target_version": dst_version,
        "target_path": "",
        "status": "resolved",
    }

    if not registry.has_schema(dst_name, dst_version):
        entry["status"] = "missing_schema"
        entry["target_path"] = _normalize_fragment(fragment)
        return entry

    if not _fragment_valid(fragment):
        entry["status"] = "invalid_pointer"
        entry["target_path"] = fragment
        return entry

    dst_document = registry.get_schema(dst_name, dst_version).document
    raw = fragment if not fragment or fragment.startswith("/") else "/" + fragment
    logical = ptr.resolve_logical(dst_document, raw)
    normalized = ptr.normalize_field_pointer(raw)
    entry["target_path"] = logical if logical is not None else normalized

    candidates = []
    for cand in (logical, normalized):
        if cand is not None and cand not in candidates:
            candidates.append(cand)
    reachable = any(
        registry.field_path_exists(dst_name, dst_version, cand)
        for cand in candidates
    )
    if not reachable:
        entry["status"] = "missing_field"
    return entry


def _issue_message(entry: dict[str, str]) -> str:
    target = f"{entry['target_schema']}@{entry['target_version']}"
    if entry["status"] == "missing_schema":
        return f"跨 Schema 引用的目标 Schema {target} 不存在"
    if entry["status"] == "invalid_pointer":
        return f"跨 Schema 引用 {target} 的片段 {entry['target_path']!r} 不是合法 JSON Pointer"
    return f"跨 Schema 引用 {target}{entry['target_path']} 的目标字段不可达"


def _select_versions(
    registry: Registry, name: str | None, version: str | None
) -> list:
    """按过滤条件选取待审计的源版本；无匹配版本时抛 NotFoundError。"""
    if name is None and version is None:
        selected = registry.all_schemas()
        if not selected:
            raise NotFoundError("没有已注册的 Schema 版本")
        return selected
    if version is None:
        # list_versions 对未知名称抛 NotFoundError；按注册顺序返回。
        return [registry.get_schema(name, v) for v in registry.list_versions(name)]
    if name is None:
        selected = [s for s in registry.all_schemas() if s.version == version]
        if not selected:
            raise NotFoundError(
                f"版本 {version} 不存在", details={"version": version}
            )
        return selected
    return [registry.get_schema(name, version)]


def check_references(
    registry: Registry, name: str | None = None, version: str | None = None
) -> dict[str, Any]:
    """审计选定 Schema 版本的跨 Schema 引用完整性（只读）。

    返回可 JSON 序列化的 dict：``checked``/``total`` 为源版本数，
    ``resolved`` 为外部引用全部可解析（含无引用）的源版本数，
    ``references`` 为稳定去重后的全部引用位点，``issues`` 为非 resolved
    项的原因与消息。相同输入结果稳定。
    """
    selected = _select_versions(registry, name, version)

    references: dict[tuple, dict[str, str]] = {}
    issues: list[dict[str, str]] = []
    issue_keys: set[tuple] = set()
    resolved_versions = 0

    for record in selected:
        version_ok = True
        for src_raw, dst_name, dst_version, fragment in _extract_external_refs(
            record.document
        ):
            entry = _audit_ref(
                registry, record.document, src_raw, dst_name, dst_version, fragment
            )
            full = {
                "source_schema": record.name,
                "source_version": record.version,
                **entry,
            }
            key = (
                record.name,
                record.version,
                entry["source_path"],
                entry["target_schema"],
                entry["target_version"],
                entry["target_path"],
            )
            references.setdefault(key, full)
            if entry["status"] != "resolved":
                version_ok = False
                if key not in issue_keys:
                    issue_keys.add(key)
                    issues.append(
                        {
                            **full,
                            "reason": entry["status"],
                            "message": _issue_message(entry),
                        }
                    )
        if version_ok:
            resolved_versions += 1

    ordered_keys = sorted(references)
    issues.sort(
        key=lambda i: (
            i["source_schema"],
            i["source_version"],
            i["source_path"],
            i["target_schema"],
            i["target_version"],
            i["target_path"],
        )
    )
    return {
        "checked": len(selected),
        "total": len(selected),
        "resolved": resolved_versions,
        "issues": issues,
        "references": [references[k] for k in ordered_keys],
    }
