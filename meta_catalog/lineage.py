"""字段血缘追溯（``trace_field_lineage``）。

沿既有的字段级比较报告，确认一个逻辑字段在同名 Schema 相邻版本间如何保留、
改名或终止，给出从起始版本到目标版本的**唯一**步进序列：

  * 报告中的变更条目按 ``old_path`` → ``new_path`` 传播；显式重命名整棵
    子树一起迁移，子树深层字段保留映射根之后的后缀（``/a/x`` 随
    ``/a`` → ``/b`` 映为 ``/b/x``）；
  * 报告没有任何条目落在该字段上、且字段仍存在于候选版本时，记为
    ``unchanged`` / ``compatible`` 的同路径步进；
  * 字段被删除且没有重命名传播时，血缘在该版本终止。

同一对相邻版本之间可能存在多份比较报告：它们对同一字段必须给出一致去向，
否则抛 :class:`FieldTraceAmbiguous`；一份报告把同一字段映向多个目标同样抛
该错，``details`` 列出冲突的 ``report_id`` 与路径。报告只形成有向迁移边，
同一条边（``report_id``）在任一路线中至多经过一次，报告环因此不可能进入
结果路线。

目标版本可以是已注册版本，也可以只是某份报告的候选版本标签；未注册标签
必然是版本图的叶子（以它为基线的报告无法生成）。

本模块只读注册簿与既有报告：不注册 Schema、不生成报告、不写入检索索引；
相同输入返回相同结构，返回值为独立副本。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable

from . import pointer as ptr
from .errors import FieldTraceAmbiguous, FieldTraceInvalid, NotFoundError
from .registry import Registry

# 候选版本字段可达性判定：入参为逻辑路径，返回在候选版本是否可达。
# 候选版本未注册（仅报告标签）时为 None，改由报告条目推断。
_Resolver = Callable[[str], bool]

# 无变化步进的种类与兼容结论。
UNCHANGED = "unchanged"
COMPATIBLE = "compatible"
DELETED = "deleted"
BREAKING = "breaking"

# 状态：(版本, 字段路径)；父指针附带所用报告与步进结论。
_State = tuple[str, str]


def trace_field_lineage(
    registry: Registry,
    reports: list[dict[str, Any]],
    name: Any,
    baseline_version: Any,
    target_version: Any,
    path: Any,
) -> dict[str, Any]:
    """追溯字段从 ``baseline_version`` 到 ``target_version`` 的唯一血缘步进。

    ``reports`` 为目录中已有的全部比较报告（本函数不生成、不改动报告）。
    成功时返回普通 dict（可直接 JSON 序列化）；请求级问题抛
    :class:`FieldTraceInvalid`，起点版本/字段、目标版本或路线不存在抛
    :class:`NotFoundError`，报告冲突或重命名目标不唯一抛
    :class:`FieldTraceAmbiguous`。
    """
    _validate_request(name, baseline_version, target_version, path)

    # 起点版本必须已注册；起点字段必须逻辑可达（口径与影响分析一致）。
    registry.get_schema(name, baseline_version)
    if not registry.logical_path_exists(name, baseline_version, path):
        raise NotFoundError(
            f"字段 {name}@{baseline_version}{path} 不可达",
            details={"schema": name, "version": baseline_version, "path": path},
        )

    # 目标版本必须是注册版本或某份报告的候选版本标签。
    known_versions = set(registry.list_versions(name))
    for report in reports:
        label = report.get("candidate_version")
        if isinstance(label, str):
            known_versions.add(label)
    if target_version not in known_versions:
        raise NotFoundError(
            f"Schema {name} 不存在版本 {target_version}，也无报告以其为候选版本",
            details={"schema": name, "version": target_version},
        )

    # 起止相同：空步进，目标路径即入参路径。
    if baseline_version == target_version:
        return _result(name, baseline_version, target_version, path, path, [])

    graph = _ReportGraph.build(registry, reports, name)
    steps, final_path = graph.walk(baseline_version, path, target_version)
    return _result(name, baseline_version, target_version, path, final_path, steps)


# --------------------------------------------------------------- 请求校验
def _validate_request(
    name: Any, baseline_version: Any, target_version: Any, path: Any
) -> None:
    """请求级结构校验，失败统一抛 :class:`FieldTraceInvalid`。"""
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
                details={"reason": "invalid_version", "field": label, "value": version},
            )
    if not isinstance(path, str):
        raise FieldTraceInvalid(
            "path 必须是 JSON Pointer 字符串",
            details={"reason": "invalid_path", "path": path},
        )
    try:
        ptr.parse(path)
    except ValueError as exc:
        raise FieldTraceInvalid(
            f"path 不是合法 JSON Pointer: {path!r}",
            details={"reason": "invalid_pointer", "path": path},
        ) from exc
    # ptr.parse 只校验前导斜杠；这里按 RFC 6901 严格校验转义（只允许 ~0/~1）。
    if not _valid_pointer_escapes(path):
        raise FieldTraceInvalid(
            f"path 不是合法 JSON Pointer（~ 后只允许 0 或 1）: {path!r}",
            details={"reason": "invalid_pointer", "path": path},
        )
    # 逻辑字段指针省略 properties 段、additionalProperties 写作 *、
    # 单 Schema items 写作 -；纯文本规整后发生变化说明传入的是文档指针。
    if ptr.normalize_field_pointer(path) != path:
        raise FieldTraceInvalid(
            f"path 不是逻辑字段指针（请省略 properties 等容器段）: {path!r}",
            details={"reason": "non_logical_pointer", "path": path},
        )


def _valid_pointer_escapes(path: str) -> bool:
    """逐段检查 JSON Pointer 转义：``~`` 后只能是 ``0`` 或 ``1``。"""
    for segment in path.split("/")[1:] if path else ():
        i = 0
        while i < len(segment):
            if segment[i] == "~":
                if i + 1 >= len(segment) or segment[i + 1] not in "01":
                    return False
                i += 2
            else:
                i += 1
    return True


# --------------------------------------------------------------- 报告迁移图
class _Outcome:
    """单份报告对某字段的传播结论。

    ``terminated`` 为真时血缘在该报告终止（删除且无重命名传播）；否则
    ``destinations`` 为去向新路径集合（正常恰好一个，多个即重命名目标
    不唯一）。``signature`` 是可跨报告比较的完整去向签名：不同签名即
    相邻版本报告冲突。
    """

    __slots__ = (
        "report_id",
        "terminated",
        "destinations",
        "change_kind",
        "compatibility",
    )

    def __init__(
        self,
        report_id: str,
        terminated: bool,
        destinations: frozenset[str],
        change_kind: str,
        compatibility: str,
    ) -> None:
        self.report_id = report_id
        self.terminated = terminated
        self.destinations = destinations
        self.change_kind = change_kind
        self.compatibility = compatibility

    @property
    def signature(self) -> tuple:
        if self.terminated:
            return ("terminated",)
        return (
            "moved",
            tuple(sorted(self.destinations)),
            self.change_kind,
            self.compatibility,
        )

    def as_conflict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "paths": [] if self.terminated else sorted(self.destinations),
        }


class _ReportEdge:
    """一份报告形成的一条相邻版本迁移边。"""

    def __init__(
        self,
        report_id: str,
        changes: list[dict[str, Any]],
        renames: list[dict[str, str]],
        candidate_resolver: _Resolver | None,
    ) -> None:
        self.report_id = report_id
        # old_path -> 变更条目（同一 old_path 在一份报告中至多一条）。
        self._exact: dict[str, dict[str, Any]] = {}
        for change in changes:
            old_path = change.get("old_path")
            if isinstance(old_path, str):
                self._exact[old_path] = change
        # 重命名根（来自报告的 renames 声明，与 rename 条目同源），按路径
        # 段长度降序以便取最深的包含根。
        self._renames = sorted(
            (
                (r["from"], r["to"])
                for r in renames
                if isinstance(r, dict)
                and isinstance(r.get("from"), str)
                and isinstance(r.get("to"), str)
            ),
            key=lambda pair: (-len(ptr.parse(pair[0])), ptr.parse(pair[0]), pair[0]),
        )
        self._candidate_resolver = candidate_resolver

    def propagate(self, path: str) -> _Outcome:
        """计算字段经本报告的去向。"""
        change = self._exact.get(path)
        if change is not None:
            new_path = change.get("new_path")
            if new_path is None:
                return self._terminated()
            return _Outcome(
                self.report_id,
                False,
                frozenset({new_path}),
                change.get("change_kind", "modified"),
                change.get("compatibility", COMPATIBLE),
            )

        root = self._rename_root(path)
        if root is not None:
            src, dst = root
            suffix = ptr.parse(path)[len(ptr.parse(src)) :]
            new_path = ptr.format(list(ptr.parse(dst)) + list(suffix))
            if not self._exists_in_candidate(new_path):
                # 映射目标在候选版本不可达：深层字段随子树删除而终止
                # （与迁移规划 target_deleted 同口径）。
                return self._terminated()
            return _Outcome(
                self.report_id, False, frozenset({new_path}), "rename", BREAKING
            )

        # 报告未涉及该字段：字段仍在候选版本即同路径保留（unchanged）。
        if self._exists_in_candidate(path):
            return _Outcome(
                self.report_id, False, frozenset({path}), UNCHANGED, COMPATIBLE
            )
        return self._terminated()

    def _rename_root(self, path: str) -> tuple[str, str] | None:
        """返回最深的、严格包含该字段的重命名根。"""
        segs = ptr.parse(path)
        for src, dst in self._renames:
            root_segs = ptr.parse(src)
            if len(root_segs) < len(segs) and segs[: len(root_segs)] == root_segs:
                return src, dst
        return None

    def _exists_in_candidate(self, path: str) -> bool:
        resolver = self._candidate_resolver
        if resolver is not None:
            # 与迁移规划一致：沿跨 Schema $ref 跳转判定逻辑可达性。
            return resolver(path)
        # 候选版本未注册（仅报告标签）：删除必然在报告中留下
        # old_path == path 的 deleted 条目；没有该条目即字段仍在候选文档。
        change = self._exact.get(path)
        return change is None or change.get("new_path") is not None

    def _terminated(self) -> _Outcome:
        return _Outcome(self.report_id, True, frozenset(), DELETED, BREAKING)


class _ReportGraph:
    """同名 Schema 报告构成的相邻版本迁移图（构建后只读）。"""

    def __init__(
        self, edges: dict[tuple[str, str], list[_ReportEdge]]
    ) -> None:
        self._edges = edges

    @classmethod
    def build(
        cls, registry: Registry, reports: list[dict[str, Any]], name: str
    ) -> "_ReportGraph":
        edges: dict[tuple[str, str], list[_ReportEdge]] = {}
        resolvers: dict[tuple[str, str], _Resolver | None] = {}
        for report in reports:
            if report.get("schema") != name:
                continue
            old_ver = report.get("baseline_version")
            new_ver = report.get("candidate_version")
            if not isinstance(old_ver, str) or not isinstance(new_ver, str):
                # 无候选版本标签的报告无法形成版本间迁移边。
                continue
            key = (old_ver, new_ver)
            if key not in resolvers:
                resolvers[key] = cls._candidate_resolver(registry, name, new_ver)
            edges.setdefault(key, []).append(
                _ReportEdge(
                    report["report_id"],
                    list(report.get("changes", [])),
                    list(report.get("renames", [])),
                    resolvers[key],
                )
            )
        for edge_list in edges.values():
            edge_list.sort(key=lambda e: e.report_id)
        return cls(edges)

    @staticmethod
    def _candidate_resolver(
        registry: Registry, name: str, version: str
    ) -> _Resolver | None:
        """候选版本已注册时返回其逻辑可达性判定；仅报告标签时返回 None。"""
        if not registry.has_schema(name, version):
            return None
        return lambda path: registry.logical_path_exists(name, version, path)

    def walk(
        self, start_version: str, start_path: str, target_version: str
    ) -> tuple[list[dict[str, Any]], str]:
        """求到达目标版本的唯一最短血缘路线，返回 ``(步进列表, 目标路径)``。

        BFS 状态为 ``(版本, 路径, 已用 report_id 集合)``：同一报告在一条
        路线中至多经过一次，报告环因此不可能进入结果。父指针在首次发现
        时确定；邻接状态按（目标版本、目标路径）升序扩展，等长路线取
        确定性最小者。
        """
        used: dict[_State, tuple[_State | None, str | None, _Outcome | None]] = {}
        start: _State = (start_version, start_path)
        used[start] = (None, None, None)
        queue: deque[tuple[_State, frozenset[str]]] = deque(
            [(start, frozenset())]
        )
        goal: _State | None = None
        while queue:
            state, reports_used = queue.popleft()
            version, path = state
            if version == target_version:
                goal = state
                break
            for nxt, report_id, outcome in self._neighbors(version, path):
                if report_id in reports_used:
                    continue  # 同一报告在路线中至多一次：报告环无效。
                if nxt in used:
                    continue
                used[nxt] = (state, report_id, outcome)
                queue.append((nxt, reports_used | {report_id}))

        if goal is None:
            raise NotFoundError(
                f"字段 {start_path} 无从 {start_version} 到 {target_version} 的血缘路线"
                "（字段可能已被删除且无重命名传播）",
                details={
                    "reason": "lineage_not_found",
                    "path": start_path,
                    "from_version": start_version,
                    "to_version": target_version,
                },
            )

        steps: list[dict[str, Any]] = []
        cur: _State = goal
        while used[cur][0] is not None:
            prev, report_id, outcome = used[cur]
            steps.append(
                {
                    "report_id": report_id,
                    "from_version": prev[0],
                    "to_version": cur[0],
                    "old_path": prev[1],
                    "new_path": cur[1],
                    "change_kind": outcome.change_kind,
                    "compatibility": outcome.compatibility,
                }
            )
            cur = prev
        steps.reverse()
        return steps, goal[1]

    def _neighbors(
        self, version: str, path: str
    ) -> list[tuple[_State, str, _Outcome]]:
        """展开某状态在全部相邻版本报告中的一致去向。

        同一对版本间的多份报告必须给出一致去向（终止也算一种去向），否则
        抛 :class:`FieldTraceAmbiguous`。去向按（目标版本、目标路径）稳定
        排序。
        """
        out: list[tuple[_State, str, _Outcome]] = []
        for (old_ver, new_ver), edge_list in sorted(self._edges.items()):
            if old_ver != version:
                continue
            chosen: _Outcome | None = None
            conflicts: list[dict[str, Any]] = []
            for edge in edge_list:
                outcome = edge.propagate(path)
                if chosen is None:
                    chosen = outcome
                elif outcome.signature != chosen.signature:
                    conflicts.append(outcome.as_conflict())
            if conflicts:
                conflicts.insert(0, chosen.as_conflict())
                raise FieldTraceAmbiguous(
                    f"版本 {old_ver} -> {new_ver} 的多份比较报告对字段 {path} "
                    "给出冲突去向，字段血缘不唯一",
                    details={
                        "reason": "conflicting_reports",
                        "from_version": old_ver,
                        "to_version": new_ver,
                        "path": path,
                        "conflicts": conflicts,
                    },
                )
            assert chosen is not None
            if chosen.terminated:
                continue  # 删除且无重命名传播：该方向无后继状态。
            for dest_path in sorted(chosen.destinations):
                out.append(((new_ver, dest_path), chosen.report_id, chosen))
        out.sort(key=lambda item: (item[0][0], ptr.parse(item[0][1]), item[0][1]))
        return out


# --------------------------------------------------------------- 结果组装
def _result(
    schema: str,
    baseline_version: str,
    target_version: str,
    path: str,
    target_path: str,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for step in steps:
        counts[step["change_kind"]] = counts.get(step["change_kind"], 0) + 1
    return {
        "schema": schema,
        "baseline_version": baseline_version,
        "target_version": target_version,
        "path": path,
        "target_path": target_path,
        "summary": {
            "steps": len(steps),
            "change_kinds": dict(sorted(counts.items())),
        },
        "steps": steps,
    }
