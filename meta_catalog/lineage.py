"""字段血缘追溯（``trace_field_lineage``）。

沿字段级比较报告确认一个逻辑字段在同一 Schema 跨版本间的命运：保留
（``unchanged`` / ``compatible``）、改名（沿报告的 ``old_path`` /
``new_path`` 与显式重命名子树传播，深层字段保留后缀）或终止（删除且无重命名
传播）。报告本身记录的变化条目沿用其 ``change_kind`` 与
``compatibility``；报告未提及的字段在候选侧仍存在时按同路径保留处理。

版本图的边是已生成的比较报告：``baseline_version -> candidate_version``。
目标版本可以是已注册版本，也可以只是某次内联候选比较的候选版本。多条报告
对同一相邻版本给出相同结论时合并为一条步进；给出互斥结论（新路径 / 变化
结论不一致，或一方保留而另一方终止）且各自能走通到目标版本时，血缘不唯一，
抛 :class:`FieldTraceAmbiguous`。

本模块只读注册簿与报告库：不注册 Schema、不生成报告、不写入检索索引；
相同输入返回相同结构，返回值为独立副本。
"""

from __future__ import annotations

from typing import Any

from . import pointer as ptr, schema_fields as sf
from .errors import FieldTraceAmbiguous, FieldTraceInvalid, NotFoundError
from .registry import Registry

UNCHANGED = "unchanged"
COMPATIBLE = "compatible"
RENAMED = "rename"
DELETED = "deleted"

STATUS_UNCHANGED = "unchanged"
STATUS_RENAMED = "renamed"
STATUS_TERMINATED = "terminated"

# 逻辑字段路径中以裸文档容器段出现时表示传入的是文档指针而非逻辑指针；
# 是否属于这种情况需结合起始文档上下文判定（属性本身可能就叫 properties）。
_CONTAINER_SEGMENTS = frozenset(
    {"properties", "prefixItems", "additionalProperties", "patternProperties"}
)

# 探索分支数量上限，防止报告图异常膨胀时无界枚举；正常目录远不会触及。
_MAX_EXPANSIONS = 20_000
_MAX_RAW_ROUTES = 100


def _validate_arguments(
    name: Any, baseline_version: Any, target_version: Any, path: Any
) -> None:
    """请求级结构校验，失败抛 :class:`FieldTraceInvalid`。"""
    if not isinstance(name, str) or not name:
        raise FieldTraceInvalid(
            "Schema 名称必须是非空字符串", details={"reason": "invalid_schema"}
        )
    for label, version in (
        ("baseline_version", baseline_version),
        ("target_version", target_version),
    ):
        if not isinstance(version, str) or not version:
            raise FieldTraceInvalid(
                f"{label} 必须是非空字符串",
                details={"reason": "invalid_version", "label": version},
            )
    if not isinstance(path, str):
        raise FieldTraceInvalid(
            "path 必须是逻辑 JSON Pointer 字符串（根路径为空串）",
            details={"reason": "invalid_path", "path": path},
        )
    try:
        ptr.parse(path)
    except ValueError as exc:
        raise FieldTraceInvalid(
            f"path 不是合法逻辑 JSONPointer: {path!r}",
            details={"reason": "invalid_pointer", "path": path},
        ) from exc


def _ensure_logical_pointer(document: Any, path: str, start_fields: Any) -> None:
    """结合起始文档判定 path 是逻辑指针；文档指针形态抛 :class:`FieldTraceInvalid`。

    ``resolve_logical`` 能把文档指针（如 ``/properties/a``）解释为另一条逻辑
    路径（``/a``），据此识别误传的文档指针；若解释不出来（例如属性恰好名为
    ``properties`` 的字面逻辑路径），只要该字面逻辑路径确实在字段表中即合法，
    其余情况留给后续字段存在性检查报 :class:`NotFoundError`。
    """
    resolved = ptr.resolve_logical(document, path)
    if resolved is not None and resolved != path:
        raise FieldTraceInvalid(
            f"path 必须是逻辑字段指针，文档指针 {path!r} 应写作 {resolved!r}",
            details={
                "reason": "non_logical_pointer",
                "path": path,
                "logical_path": resolved,
            },
        )
    if path in start_fields:
        return
    if any(seg in _CONTAINER_SEGMENTS for seg in ptr.parse(path)):
        raise FieldTraceInvalid(
            f"path 必须是逻辑字段指针，不能包含文档容器段: {path!r}",
            details={"reason": "non_logical_pointer", "path": path},
        )


class _Transition:
    """单个版本状态上的一条（可能由多份同结论报告合并的）出边。"""

    __slots__ = (
        "to_version",
        "new_path",
        "change_kind",
        "compatibility",
        "witnesses",
    )

    def __init__(
        self,
        to_version: str,
        new_path: str | None,
        change_kind: str,
        compatibility: str,
        witnesses: tuple[str, ...],
    ) -> None:
        self.to_version = to_version
        self.new_path = new_path
        self.change_kind = change_kind
        self.compatibility = compatibility
        self.witnesses = witnesses

    def key(self) -> tuple:
        return (
            self.to_version,
            self.new_path,
            self.change_kind,
            self.compatibility,
        )

    def sort_key(self) -> tuple:
        # 终止步 new_path 为 None，以空串占位参与确定性排序（排在同前缀最后）。
        return self.key()[:1] + (self.new_path or "",) + self.key()[2:] + (
            self.witnesses[0],
        )


def _candidate_fields(
    registry: Registry,
    name: str,
    report: dict[str, Any],
    candidate_docs: dict[str, Any],
) -> dict[str, sf.Field] | None:
    """报告候选侧字段表：优先已注册版本，否则用比较时留存的内联候选文档。"""
    to_version = report["candidate_version"]
    if to_version is not None and registry.has_schema(name, to_version):
        return sf.expand(registry.get_schema(name, to_version).document)
    doc = candidate_docs.get(report["report_id"])
    return sf.expand(doc) if doc is not None else None


def _rename_destination(report: dict[str, Any], path: str) -> str | None:
    """若路径落在某条显式重命名子树内，返回保留后缀后的新路径；否则 None。"""
    segs = ptr.parse(path)
    best: tuple[str, str] | None = None
    for item in report.get("renames", []):
        root, dst = item["from"], item["to"]
        rsegs = ptr.parse(root)
        if len(rsegs) < len(segs) and segs[: len(rsegs)] == rsegs:
            if best is None or len(rsegs) > len(ptr.parse(best[0])):
                best = (root, dst)
    if best is None:
        return None
    suffix = segs[len(ptr.parse(best[0])) :]
    return ptr.format(list(ptr.parse(best[1])) + list(suffix))


def _outcomes_for_report(
    registry: Registry,
    name: str,
    path: str,
    report: dict[str, Any],
    candidate_docs: dict[str, Any],
) -> list[_Transition]:
    """单份报告对当前字段给出的结论（正常 0 或 1 条，删除为终止 1 条）。"""
    to_version = report["candidate_version"]

    matched = [c for c in report["changes"] if c.get("old_path") == path]

    if matched:
        out: list[_Transition] = []
        for c in matched:
            out.append(
                _Transition(
                    to_version,
                    c.get("new_path"),
                    c["change_kind"],
                    c["compatibility"],
                    (report["report_id"],),
                )
            )
        return out

    moved = _rename_destination(report, path)
    if moved is not None:
        # 重命名子树深层字段：报告未单列变化说明结构与必填均未变，路径随显式
        # 重命名整体迁移，按一次 rename / breaking 传播；候选侧已不存在则
        # 比较阶段会生成 deleted 条目（已在上面的 matched 分支处理）。
        to_fields = _candidate_fields(registry, name, report, candidate_docs)
        if to_fields is None or moved in to_fields:
            return [
                _Transition(
                    to_version, moved, RENAMED, "breaking", (report["report_id"],)
                )
            ]
        return [
            _Transition(
                to_version, None, DELETED, "breaking", (report["report_id"],)
            )
        ]

    # 报告未提及该字段：候选侧仍存在即同路径保留；候选文档不可考时按报告
    # 未记录变化处理（终局存在性由最后抵达的版本侧字段表保证）。
    to_fields = _candidate_fields(registry, name, report, candidate_docs)
    if to_fields is None or path in to_fields:
        return [
            _Transition(
                to_version, path, UNCHANGED, COMPATIBLE, (report["report_id"],)
            )
        ]
    return [_Transition(to_version, None, DELETED, "breaking", (report["report_id"],))]


def _merge_transitions(transitions: list[_Transition]) -> list[_Transition]:
    """合并同结论（目标版本 / 新路径 / 变化 / 兼容性一致）的多份报告。"""
    merged: dict[tuple, _Transition] = {}
    for t in transitions:
        existing = merged.get(t.key())
        if existing is None:
            merged[t.key()] = _Transition(
                t.to_version, t.new_path, t.change_kind, t.compatibility,
                tuple(sorted(t.witnesses)),
            )
        else:
            witnesses = tuple(sorted(set(existing.witnesses) | set(t.witnesses)))
            existing.witnesses = witnesses
    return sorted(merged.values(), key=lambda t: t.sort_key())


def trace_field_lineage(
    registry: Registry,
    reports: dict[str, dict[str, Any]],
    candidate_docs: dict[str, Any],
    name: Any,
    baseline_version: Any,
    target_version: Any,
    path: Any,
) -> dict[str, Any]:
    """追溯字段从起始版本到目标版本的唯一步进链（只读）。

    结构非法抛 :class:`FieldTraceInvalid`；起始版本 / 字段不存在、无路线到
    目标版本、字段在途中删除且无重命名传播抛 :class:`NotFoundError`；相邻
    版本报告冲突或重命名目标不唯一（多条互斥路线均可到达目标版本）抛
    :class:`FieldTraceAmbiguous`。
    """
    _validate_arguments(name, baseline_version, target_version, path)

    # 起始版本与字段必须存在（起始版本限定为已注册版本；字段以同名字段表为准）。
    start_record = registry.get_schema(name, baseline_version)
    start_fields = sf.expand(start_record.document)
    _ensure_logical_pointer(start_record.document, path, start_fields)
    if path not in start_fields:
        raise NotFoundError(
            f"字段 {name}@{baseline_version}{path} 不存在",
            details={"schema": name, "version": baseline_version, "path": path},
        )

    # 起止相同：空步进，target_path 等于 path。
    if baseline_version == target_version:
        return _build_result(name, baseline_version, target_version, path, [])

    # 该 Schema 的全部报告作为版本图的边：baseline_version -> candidate_version。
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for rid in sorted(reports):
        report = reports[rid]
        if report.get("schema") == name:
            outgoing.setdefault(report["baseline_version"], []).append(report)

    expansions = 0

    def transitions_at(version: str, cur_path: str) -> list[_Transition]:
        raw: list[_Transition] = []
        for report in outgoing.get(version, ()):
            raw.extend(
                _outcomes_for_report(
                    registry, name, cur_path, report, candidate_docs
                )
            )
        return _merge_transitions(raw)

    # DFS 枚举到达 target_version 的路线。路线状态为 (版本, 当前路径)，
    # 终止步的新路径为 None；同一路线不重复状态（环无效），每份 report_id
    # 至多使用一次。
    raw_routes: list[list[tuple[_Transition, str]]] = []
    limit_hit = False

    def walk(
        version: str,
        cur_path: str | None,
        states: list[tuple[str, str | None]],
        used_reports: frozenset[str],
        route: list[tuple[_Transition, str]],
    ) -> None:
        nonlocal expansions, limit_hit
        if version == target_version:
            raw_routes.append(list(route))
            return
        if cur_path is None:
            return  # 在到达目标版本之前终止，无法继续传播。
        expansions += 1
        if expansions > _MAX_EXPANSIONS:
            limit_hit = True
            return
        for t in transitions_at(version, cur_path):
            witness = next((r for r in t.witnesses if r not in used_reports), None)
            if witness is None:
                continue  # 该合并边的每份报告在本路线中都已使用过。
            next_state = (t.to_version, t.new_path)
            if next_state in states:
                continue  # 环无效。
            walk(
                t.to_version,
                t.new_path,
                states + [next_state],
                used_reports | {witness},
                route + [(t, witness)],
            )
            if limit_hit or len(raw_routes) >= _MAX_RAW_ROUTES:
                return

    walk(
        baseline_version, path, [(baseline_version, path)], frozenset(), []
    )

    if limit_hit:
        raise FieldTraceAmbiguous(
            "字段血缘可选路线过多，无法给出唯一步进",
            details={"reason": "too_many_routes"},
        )

    # 语义去重：步进序列（目标版本 / 新路径 / 变化 / 兼容性）一致即同一条血缘，
    # 平行同结论报告不构成冲突。
    distinct: list[list[tuple[_Transition, str]]] = []
    seen: set[tuple] = set()
    for route in raw_routes:
        signature = tuple(t.key() for t, _ in route)
        if signature in seen:
            continue
        seen.add(signature)
        distinct.append(route)

    if not distinct:
        raise NotFoundError(
            f"字段 {name}@{baseline_version}{path} 无到达版本 {target_version} 的血缘路线",
            details={
                "schema": name,
                "baseline_version": baseline_version,
                "target_version": target_version,
                "path": path,
                "reason": "no_lineage_route",
            },
        )

    if len(distinct) > 1:
        raise FieldTraceAmbiguous(
            f"字段 {name}@{baseline_version}{path} 到 {target_version} 的血缘不唯一",
            details={
                "reason": "conflicting_reports",
                "conflicts": _conflict_details(baseline_version, distinct),
            },
        )

    return _build_result(
        name, baseline_version, target_version, path, distinct[0]
    )


def _conflict_details(
    baseline_version: str, routes: list[list[tuple[_Transition, str]]]
) -> list[dict[str, Any]]:
    """定位各路线第一处分歧，列出冲突的 report_id 与路径。"""
    divergence = 0
    max_len = max(len(r) for r in routes)
    while divergence < max_len:
        keys = {
            r[divergence][0].key() for r in routes if divergence < len(r)
        }
        if len(keys) > 1:
            break
        divergence += 1

    conflicts: list[dict[str, Any]] = []
    for route in routes:
        if divergence >= len(route):
            continue
        t, witness = route[divergence]
        conflicts.append(
            {
                "report_id": witness,
                "from_version": (
                    route[divergence - 1][0].to_version
                    if divergence
                    else baseline_version
                ),
                "to_version": t.to_version,
                "old_path": route[divergence - 1][0].new_path
                if divergence
                else None,
                "new_path": t.new_path,
                "change_kind": t.change_kind,
                "compatibility": t.compatibility,
            }
        )
    conflicts.sort(key=lambda c: (c["report_id"], c["new_path"] or ""))
    return conflicts


def _build_result(
    name: str,
    baseline_version: str,
    target_version: str,
    start_path: str,
    route: list[tuple[_Transition, str]],
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    from_version = baseline_version
    old_path = start_path
    target_path: str | None = start_path
    changed = False
    for t, witness in route:
        steps.append(
            {
                "report_id": witness,
                "from_version": from_version,
                "to_version": t.to_version,
                "old_path": old_path,
                "new_path": t.new_path,
                "change_kind": t.change_kind,
                "compatibility": t.compatibility,
            }
        )
        from_version = t.to_version
        old_path = t.new_path
        target_path = t.new_path
        if t.change_kind != UNCHANGED:
            changed = True

    if target_path is None:
        status = STATUS_TERMINATED
    elif changed:
        status = STATUS_RENAMED
    else:
        status = STATUS_UNCHANGED

    return {
        "schema": name,
        "baseline_version": baseline_version,
        "target_version": target_version,
        "path": start_path,
        "target_path": target_path,
        "summary": {
            "status": status,
            "steps": len(steps),
            "changes": sum(1 for t, _ in route if t.change_kind != UNCHANGED),
        },
        "steps": steps,
    }
