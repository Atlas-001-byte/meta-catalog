"""字段级 Schema 变更比较。

变更分类（每条结果只有一个确定种类与一个兼容结论）：

兼容：``added`` / ``enum_relaxed`` / ``type_widened`` /
``default_added`` / ``constraint_relaxed`` / ``required_relaxed``
破坏：``removed`` / ``added_required`` / ``renamed`` /
``required_added`` / ``nullable_added`` / ``enum_narrowed`` /
``type_narrowed`` / ``type_changed`` / ``const_changed`` /
``constraint_narrowed`` / ``constraint_changed`` / ``ref_changed``
元数据：``metadata_changed``
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import ImpactAnalysisTooLarge, SchemaComparisonInvalid
from .impact import ImpactAnalyzer, ImpactedAsset
from .json_schema import (
    FieldConstraints,
    SchemaShapeError,
    constraint_signature,
    meta_signature,
    prepare,
    summarize,
)
from .limits import LIMITS
from .registry import Registry, SchemaRecord


class ChangeKind(str, Enum):
    ADDED = "added"                              # 新增非必填属性（兼容）
    ADDED_REQUIRED = "added_required"            # 新增必填属性（破坏）
    REMOVED = "removed"                          # 删除属性（破坏）
    RENAMED = "renamed"                          # 显式重命名（破坏）
    REQUIRED_ADDED = "required_added"            # 既有属性新增必填约束（破坏）
    REQUIRED_RELAXED = "required_relaxed"        # 既有属性取消必填（兼容）
    NULLABLE_ADDED = "nullable_added"            # 开始接受原本不接受的 null（破坏）
    TYPE_NARROWED = "type_narrowed"              # 收窄类型（破坏）
    TYPE_WIDENED = "type_widened"                # 放宽类型，含 integer→number（兼容）
    TYPE_CHANGED = "type_changed"                # 互不包含的类型变化（破坏）
    ENUM_NARROWED = "enum_narrowed"              # 收窄枚举（破坏）
    ENUM_RELAXED = "enum_relaxed"                # 放宽枚举（兼容）
    CONST_CHANGED = "const_changed"              # const 变化（破坏）
    CONSTRAINT_NARROWED = "constraint_narrowed"  # 数值/长度/正则收窄（破坏）
    CONSTRAINT_RELAXED = "constraint_relaxed"    # 边界放宽、format 变化（兼容）
    CONSTRAINT_CHANGED = "constraint_changed"    # 同时收窄与放宽（破坏）
    REF_CHANGED = "ref_changed"                  # 跨 Schema 引用变化（破坏）
    DEFAULT_ADDED = "default_added"              # 增加默认值（兼容）
    METADATA_CHANGED = "metadata_changed"        # 仅标题/描述/注释变化


class Compatibility(str, Enum):
    COMPATIBLE = "compatible"
    BREAKING = "breaking"
    METADATA = "metadata"


_COMPAT_BY_KIND = {
    ChangeKind.ADDED: Compatibility.COMPATIBLE,
    ChangeKind.ADDED_REQUIRED: Compatibility.BREAKING,
    ChangeKind.REMOVED: Compatibility.BREAKING,
    ChangeKind.RENAMED: Compatibility.BREAKING,
    ChangeKind.REQUIRED_ADDED: Compatibility.BREAKING,
    ChangeKind.REQUIRED_RELAXED: Compatibility.COMPATIBLE,
    ChangeKind.NULLABLE_ADDED: Compatibility.BREAKING,
    ChangeKind.TYPE_NARROWED: Compatibility.BREAKING,
    ChangeKind.TYPE_WIDENED: Compatibility.COMPATIBLE,
    ChangeKind.TYPE_CHANGED: Compatibility.BREAKING,
    ChangeKind.ENUM_NARROWED: Compatibility.BREAKING,
    ChangeKind.ENUM_RELAXED: Compatibility.COMPATIBLE,
    ChangeKind.CONST_CHANGED: Compatibility.BREAKING,
    ChangeKind.CONSTRAINT_NARROWED: Compatibility.BREAKING,
    ChangeKind.CONSTRAINT_RELAXED: Compatibility.COMPATIBLE,
    ChangeKind.CONSTRAINT_CHANGED: Compatibility.BREAKING,
    ChangeKind.REF_CHANGED: Compatibility.BREAKING,
    ChangeKind.DEFAULT_ADDED: Compatibility.COMPATIBLE,
    ChangeKind.METADATA_CHANGED: Compatibility.METADATA,
}

# 下界类约束：值变大 = 收窄
_MIN_BOUND_KEYS = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
# 上界类约束：值变大 = 放宽
_MAX_BOUND_KEYS = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")


@dataclass(frozen=True)
class RenameMapping:
    old_path: str
    new_path: str


@dataclass
class ChangeEntry:
    path: str
    kind: ChangeKind
    old_summary: dict[str, Any] | None
    new_summary: dict[str, Any] | None
    renamed_to: str | None = None
    # 影响计算所用的版本侧：("baseline" | "candidate", path)
    impact_side: tuple[str, str] = ("baseline", "")
    impacted: list[ImpactedAsset] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "field_path": self.path or "/",
            "kind": self.kind.value,
            "compatibility": _COMPAT_BY_KIND[self.kind].value,
            "old": self.old_summary,
            "new": self.new_summary,
        }
        if self.renamed_to is not None:
            out["renamed_to"] = self.renamed_to
        if self.impacted is not None:
            out["impacted_assets"] = [a.to_dict() for a in self.impacted]
        return out


def normalize_path(path: str) -> str:
    """接受 ``/a/b``、``a/b``、``#/a/b`` 写法，统一为前导 ``/``；根为 ``""``。"""
    if not isinstance(path, str):
        raise SchemaComparisonInvalid("rename path must be a string")
    if path in ("", "/", "#", "#/"):
        return ""
    if path.startswith("#"):
        path = path[1:]
    if not path.startswith("/"):
        path = "/" + path
    return path


# ---------------------------------------------------------------------------
# 同路径差异分类
# ---------------------------------------------------------------------------

def _type_diff(old_types: set[str], new_types: set[str]) -> ChangeKind | None:
    old_core = old_types - {"null"}
    new_core = new_types - {"null"}
    if old_core == new_core:
        return None
    if old_core == {"integer"} and new_core == {"number"}:
        return ChangeKind.TYPE_WIDENED
    if old_core == {"number"} and new_core == {"integer"}:
        return ChangeKind.TYPE_NARROWED
    if old_core and old_core < new_core:
        return ChangeKind.TYPE_WIDENED
    if new_core and new_core < old_core:
        return ChangeKind.TYPE_NARROWED
    if not old_core or not new_core:
        # 旧无类型约束（接受任意值）-> 新限定类型 = 收窄；反之放宽
        return ChangeKind.TYPE_NARROWED if not old_core else ChangeKind.TYPE_WIDENED
    return ChangeKind.TYPE_CHANGED


def _enum_diff(old_enum: tuple[Any, ...] | None, new_enum: tuple[Any, ...] | None) -> ChangeKind | None:
    if old_enum == new_enum:
        return None
    old_set = set(old_enum) if old_enum is not None else None
    new_set = set(new_enum) if new_enum is not None else None
    if old_set is None and new_set is not None:
        return ChangeKind.ENUM_NARROWED
    if old_set is not None and new_set is None:
        return ChangeKind.ENUM_RELAXED
    assert old_set is not None and new_set is not None
    if old_set < new_set:
        return ChangeKind.ENUM_RELAXED
    # 收窄或互不包含均为破坏
    return ChangeKind.ENUM_NARROWED


def _bound_value(fc: FieldConstraints, key: str) -> Any:
    return fc.numbers.get(key) if key in fc.numbers else fc.lengths.get(key)


def _bounds_direction(old: FieldConstraints, new: FieldConstraints) -> str | None:
    directions: set[str] = set()

    def collect(keys: tuple[str, ...], bigger_is: str) -> None:
        for key in keys:
            o, n = _bound_value(old, key), _bound_value(new, key)
            if o == n:
                continue
            if o is None or n is None:
                # 新增约束 = 收窄；移除约束 = 放宽
                directions.add("tighten" if n is not None else "relax")
            elif n > o:
                directions.add(bigger_is)
            else:
                directions.add("relax" if bigger_is == "tighten" else "tighten")

    collect(_MIN_BOUND_KEYS, "tighten")
    collect(_MAX_BOUND_KEYS, "relax")
    if not directions:
        return None
    if len(directions) == 2:
        return "mixed"
    return next(iter(directions))


def _classify_common(old: FieldConstraints, new: FieldConstraints) -> ChangeKind | None:
    """对同路径旧/新约束分类；完全无差异返回 None。"""
    same_constraints = constraint_signature(old) == constraint_signature(new)
    same_meta = meta_signature(old) == meta_signature(new)
    if same_constraints and same_meta:
        return None
    if same_constraints:
        return ChangeKind.METADATA_CHANGED

    old_types, new_types = set(old.types), set(new.types)
    old_core = old_types - {"null"}
    new_core = new_types - {"null"}

    # 破坏类维度按固定优先级判定（一条结果只给一个结论）
    if not old.required and new.required:
        return ChangeKind.REQUIRED_ADDED
    # 仅当旧版确实限定了非空类型集合（即原本不接受 null）时，
    # 新增 null 才计为 nullable_added。
    if old_core and "null" not in old_types and "null" in new_types:
        return ChangeKind.NULLABLE_ADDED

    type_kind = _type_diff(old_types, new_types)
    if type_kind in (ChangeKind.TYPE_NARROWED, ChangeKind.TYPE_CHANGED):
        return type_kind

    enum_kind = _enum_diff(old.enum, new.enum)
    if enum_kind is ChangeKind.ENUM_NARROWED:
        return enum_kind

    if old.const != new.const:
        return ChangeKind.CONST_CHANGED

    bound_dir = _bounds_direction(old, new)
    pattern_tightened = new.pattern is not None and old.pattern != new.pattern
    pattern_relaxed = old.pattern is not None and new.pattern is None
    if bound_dir == "mixed" or (pattern_tightened and (bound_dir == "relax" or pattern_relaxed)):
        return ChangeKind.CONSTRAINT_CHANGED
    if bound_dir == "tighten" or pattern_tightened:
        return ChangeKind.CONSTRAINT_NARROWED

    if sorted(old.external_refs) != sorted(new.external_refs):
        return ChangeKind.REF_CHANGED

    # 以下均为兼容
    if type_kind is ChangeKind.TYPE_WIDENED:
        return type_kind
    if enum_kind is ChangeKind.ENUM_RELAXED:
        return enum_kind
    if old.required and not new.required:
        return ChangeKind.REQUIRED_RELAXED
    if not old.has_default and new.has_default:
        return ChangeKind.DEFAULT_ADDED
    # default 改写/移除、format 变化、上界放宽、pattern 移除等
    return ChangeKind.CONSTRAINT_RELAXED


# ---------------------------------------------------------------------------
# Rename 映射校验与跨版本一致性（并查集）
# ---------------------------------------------------------------------------

NodePair = tuple[str, str, str, str]


class RenameLineage:
    """从既有成功报告汇总的跨版本字段身份。

    节点为 ``(版本, 路径)``；一次 rename 把两端归入同一身份。
    同一身份在同一版本上出现两个不同路径即视为跨版本不一致。
    """

    def __init__(self) -> None:
        self._pairs: list[NodePair] = []

    def record(self, baseline: str, candidate: str, pairs: list[tuple[str, str]]) -> None:
        for old_path, new_path in pairs:
            self._pairs.append((baseline, old_path, candidate, new_path))

    def verify(self, mappings: list[RenameMapping], baseline: str, candidate: str) -> None:
        parent: dict[tuple[str, str], tuple[str, str]] = {}
        members: dict[tuple[str, str], set[tuple[str, str]]] = {}

        def find(x: tuple[str, str]) -> tuple[str, str]:
            root = parent.get(x, x)
            if root != x:
                root = find(root)
                parent[x] = root
            return root

        def union(a: tuple[str, str], b: tuple[str, str]) -> None:
            ra, rb = find(a), find(b)
            if ra == rb:
                root = ra
            else:
                root, child = min(ra, rb), max(ra, rb)
                parent[child] = root
                members[root] |= members.pop(child, set())
            per_version: dict[str, set[str]] = {}
            for ver, path in members[root]:
                per_version.setdefault(ver, set()).add(path)
            dup = {ver: paths for ver, paths in per_version.items() if len(paths) > 1}
            if dup:
                ver, paths = sorted(dup.items())[0]
                raise SchemaComparisonInvalid(
                    "rename mapping is inconsistent with renames recorded across "
                    f"versions: identity at version {ver} has multiple paths {sorted(paths)}"
                )

        def add_node(x: tuple[str, str]) -> None:
            if x not in parent:
                parent[x] = x
                members[x] = {x}

        all_pairs: list[NodePair] = list(self._pairs)
        all_pairs.extend((baseline, m.old_path, candidate, m.new_path) for m in mappings)
        for ver_a, path_a, ver_b, path_b in all_pairs:
            a, b = (ver_a, path_a), (ver_b, path_b)
            add_node(a)
            add_node(b)
            union(a, b)


def validate_renames(
    renames: list[dict[str, str] | tuple[str, str] | RenameMapping] | None,
    old_fields: dict[str, FieldConstraints],
    new_fields: dict[str, FieldConstraints],
    lineage: RenameLineage,
    baseline_version: str,
    candidate_version: str,
) -> list[RenameMapping]:
    """校验并规范化 rename 映射。

    抛出 :class:`SchemaComparisonInvalid` 的情形：起点/终点不存在、
    同一路径被重复映射、与历史报告跨版本不一致。
    """
    result: list[RenameMapping] = []
    seen_old: set[str] = set()
    seen_new: set[str] = set()
    for item in renames or []:
        if isinstance(item, RenameMapping):
            old_path, new_path = item.old_path, item.new_path
        elif isinstance(item, dict):
            old_path = item.get("from", item.get("old_path", ""))
            new_path = item.get("to", item.get("new_path", ""))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            old_path, new_path = item
        else:
            raise SchemaComparisonInvalid("invalid rename mapping entry")
        old_path = normalize_path(old_path)
        new_path = normalize_path(new_path)
        if old_path == "" or new_path == "":
            raise SchemaComparisonInvalid("rename endpoints must not be the root path")
        if old_path == new_path:
            raise SchemaComparisonInvalid(f"rename endpoints must differ: {old_path}")
        if old_path not in old_fields:
            raise SchemaComparisonInvalid(
                f"rename source does not exist in baseline schema: {old_path}"
            )
        if new_path not in new_fields:
            raise SchemaComparisonInvalid(
                f"rename target does not exist in candidate schema: {new_path}"
            )
        if old_path in seen_old or new_path in seen_new:
            raise SchemaComparisonInvalid(
                f"path mapped more than once: {old_path if old_path in seen_old else new_path}"
            )
        seen_old.add(old_path)
        seen_new.add(new_path)
        result.append(RenameMapping(old_path, new_path))

    result.sort(key=lambda m: (m.old_path, m.new_path))
    lineage.verify(result, baseline_version, candidate_version)
    return result


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def compare_records(
    registry: Registry,
    baseline: SchemaRecord,
    candidate: SchemaRecord,
    renames_raw: list[Any] | None,
    lineage: RenameLineage,
) -> dict[str, Any]:
    if baseline.name != candidate.name:
        raise SchemaComparisonInvalid("baseline and candidate must be the same schema")

    old_fields, new_fields = baseline.fields, candidate.fields
    if len(set(old_fields) | set(new_fields)) > LIMITS["max_fields_per_comparison"]:
        raise ImpactAnalysisTooLarge("comparison object exceeds public field limit")

    mappings = validate_renames(
        renames_raw, old_fields, new_fields, lineage,
        baseline.version, candidate.version,
    )
    pairs = [(m.old_path, m.new_path) for m in mappings]
    mapped_old = {m.old_path for m in mappings}
    mapped_new = {m.new_path for m in mappings}

    entries: list[ChangeEntry] = []

    # rename：一次 rename 一条结果（不再同时计删除 + 新增）
    for m in mappings:
        entries.append(
            ChangeEntry(
                path=m.old_path,
                kind=ChangeKind.RENAMED,
                old_summary=summarize(old_fields[m.old_path]),
                new_summary=summarize(new_fields[m.new_path]),
                renamed_to=m.new_path,
                impact_side=("baseline", m.old_path),
            )
        )

    old_paths, new_paths = set(old_fields), set(new_fields)
    # 未被 rename 消费的两侧身份
    remaining_old = old_paths - mapped_old
    remaining_new = new_paths - mapped_new

    # 删除：旧身份在新侧没有同路径身份承接。即使新侧同路径被另一条
    # rename 终点占用（不同身份），旧身份仍单独计为 removed。
    for path in sorted(remaining_old - remaining_new):
        entries.append(
            ChangeEntry(
                path=path,
                kind=ChangeKind.REMOVED,
                old_summary=summarize(old_fields[path]),
                new_summary=None,
                impact_side=("baseline", path),
            )
        )

    # 新增：新身份在旧侧不存在（含 rename 腾空后有新字段占用同路径）
    for path in sorted(p for p in remaining_new if p not in remaining_old):
        fc = new_fields[path]
        entries.append(
            ChangeEntry(
                path=path,
                kind=ChangeKind.ADDED_REQUIRED if fc.required else ChangeKind.ADDED,
                old_summary=None,
                new_summary=summarize(fc),
                impact_side=("candidate", path),
            )
        )

    # 共有路径
    for path in sorted(remaining_old & remaining_new):
        kind = _classify_common(old_fields[path], new_fields[path])
        if kind is not None:
            entries.append(
                ChangeEntry(
                    path=path,
                    kind=kind,
                    old_summary=summarize(old_fields[path]),
                    new_summary=summarize(new_fields[path]),
                    impact_side=("baseline", path),
                )
            )

    # 影响分析：按版本侧批量一次 BFS，每条变更取自己字段路径的结果
    analyzer = ImpactAnalyzer(registry)
    side_versions = {"baseline": baseline.version, "candidate": candidate.version}
    side_paths: dict[str, list[str]] = {"baseline": [], "candidate": []}
    for entry in entries:
        side_paths[entry.impact_side[0]].append(entry.impact_side[1])
    side_impacts = {
        side: analyzer.analyze(baseline.name, side_versions[side], paths)
        for side, paths in side_paths.items() if paths
    }
    for entry in entries:
        side_name, impact_path = entry.impact_side
        entry.impacted = side_impacts[side_name].get(impact_path, [])

    entries.sort(key=lambda e: (e.path, e.kind.value, e.renamed_to or ""))

    fingerprint = "\n".join(f"{a}=>{b}" for a, b in pairs)
    digest_src = "\x1f".join([
        baseline.name,
        baseline.version,
        candidate.version,
        baseline.canonical,
        candidate.canonical,
        fingerprint,
    ])
    report_id = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:32]

    return {
        "report_id": report_id,
        "schema": baseline.name,
        "baseline_version": baseline.version,
        "candidate_version": candidate.version,
        "renames": [list(p) for p in pairs],
        "changes": [e.to_dict() for e in entries],
    }


def build_ephemeral_record(name: str, version: str, raw: Any) -> SchemaRecord:
    """为内联候选文档构建不落盘、不入注册表的记录。"""
    import copy

    from .json_schema import canonical_json

    try:
        doc, fields, _ = prepare(raw)
    except SchemaShapeError as exc:
        raise SchemaComparisonInvalid(str(exc)) from exc
    return SchemaRecord(
        name=name,
        version=version,
        doc=copy.deepcopy(doc),
        fields=fields,
        canonical=canonical_json(doc),
        edges=[],
    )
