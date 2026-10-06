"""只读引用迁移规划（``plan_reference_migration``）。

同名 Schema 从基线版本切换到候选版本（两者均已注册）时，为指向基线版本
的引用逐条给出迁移建议。引用来源有两类：

  * 已注册 Schema 中指向基线版本的跨 Schema ``$ref``；
  * 资产 refs 中对该版本字段的直接引用。

深层引用保留后缀（重命名子树按后缀对齐映射），不同来源的引用不合并；
同一逻辑定位的重复引用按定位去重后稳定排序。

每条引用的迁移状态（``status``）与唯一原因（``reason``）：

  * ``ready``  / ``path_unchanged``         目标路径未变且在候选版本中可达；
  * ``renamed`` / ``path_renamed``          显式映射或重命名子树改变了目标；
  * ``broken`` / ``target_deleted``         基线字段可达，但候选版本删除了
                                            该字段且没有有效映射；
  * ``broken`` / ``baseline_target_missing`` 引用在基线版本就不可达。

本模块只读注册簿：不注册资源、不生成报告、不写入检索索引；相同输入返回
相同结构，返回值不与注册内容共享可变引用。
"""

from __future__ import annotations

from typing import Any

from . import limits, pointer as ptr, schema_fields as sf
from .audit import _build_edges
from .errors import (
    ImpactAnalysisTooLarge,
    SchemaComparisonInvalid,
)
from .registry import Registry

READY = "ready"
RENAMED = "renamed"
BROKEN = "broken"

_REASON_UNCHANGED = "path_unchanged"
_REASON_RENAMED = "path_renamed"
_REASON_DELETED = "target_deleted"
_REASON_BASELINE_MISSING = "baseline_target_missing"


def _validate_identifiers(
    name: Any, baseline_version: Any, candidate_version: Any
) -> None:
    """标识结构校验：名称与版本必须是字符串，两版本不得相同。"""
    if not isinstance(name, str):
        raise SchemaComparisonInvalid(
            "Schema 名称必须是字符串", details={"reason": "invalid_name"}
        )
    for label, value in (
        ("baseline_version", baseline_version),
        ("candidate_version", candidate_version),
    ):
        if not isinstance(value, str):
            raise SchemaComparisonInvalid(
                f"{label} 必须是字符串",
                details={"reason": "invalid_version"},
            )
    if baseline_version == candidate_version:
        raise SchemaComparisonInvalid(
            "基线版本与候选版本相同，无需迁移规划",
            details={"reason": "same_version", "version": baseline_version},
        )


def _parse_renames(renames: Any) -> list[tuple[str, str]]:
    """重命名映射结构校验：列表形态、键齐全、路径为合法逻辑 JSONPointer、
    旧路径不重复且不同旧路径不映向同一新路径。"""
    if renames is None:
        return []
    if not isinstance(renames, list):
        raise SchemaComparisonInvalid(
            "重命名映射必须是 [{from, to}, ...] 列表",
            details={"reason": "renames_not_list"},
        )

    pairs: list[tuple[str, str]] = []
    for i, item in enumerate(renames):
        if isinstance(item, dict):
            src, dst = item.get("from"), item.get("to")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            src, dst = item[0], item[1]
        else:
            src = dst = None
        if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst:
            raise SchemaComparisonInvalid(
                f"第 {i} 条重命名映射必须包含非空字符串 from 与 to",
                details={"reason": "rename_invalid", "index": i},
            )
        for ep in (src, dst):
            try:
                ptr.parse(ep)
            except ValueError as exc:
                raise SchemaComparisonInvalid(
                    f"重命名路径不是合法逻辑 JSONPointer: {ep!r}",
                    details={"reason": "invalid_pointer", "index": i, "path": ep},
                ) from exc
        pairs.append((src, dst))

    seen_from: set[str] = set()
    for src, _ in pairs:
        if src in seen_from:
            raise SchemaComparisonInvalid(
                f"旧路径 {src} 在重命名映射中重复出现",
                details={"reason": "rename_duplicate", "path": src},
            )
        seen_from.add(src)
    seen_to: set[str] = set()
    for _, dst in pairs:
        if dst in seen_to:
            raise SchemaComparisonInvalid(
                f"多个旧路径映向同一新路径 {dst}",
                details={"reason": "rename_duplicate", "path": dst},
            )
        seen_to.add(dst)
    return pairs


def _check_rename_fields(
    pairs: list[tuple[str, str]],
    baseline_fields: dict[str, sf.Field],
    candidate_fields: dict[str, sf.Field],
) -> None:
    """重命名端点必须落在对应版本的字段集合内。"""
    for src, dst in pairs:
        if src not in baseline_fields:
            raise SchemaComparisonInvalid(
                f"重命名起点 {src} 在基线版本中不存在",
                details={"reason": "rename_from_not_found", "path": src},
            )
        if dst not in candidate_fields:
            raise SchemaComparisonInvalid(
                f"重命名终点 {dst} 在候选版本中不存在",
                details={"reason": "rename_to_not_found", "path": dst},
            )


def _rename_ancestors(path: str, rename_froms: list[str]) -> list[str]:
    """包含该路径（含自身）的全部重命名根，按路径段序稳定排序。"""
    hits = [root for root in rename_froms if ptr.is_under(root, path)]
    return sorted(hits, key=lambda p: ptr.parse(p))


def _plan_target(
    registry: Registry,
    name: str,
    baseline_version: str,
    candidate_version: str,
    path: str,
    rename_froms: list[str],
    rename_map: dict[str, str],
) -> tuple[str, str, str | None]:
    """判定单条引用的迁移结论，返回 (status, reason, 建议目标路径)。"""
    ancestors = _rename_ancestors(path, rename_froms)
    if len(ancestors) > 1:
        raise SchemaComparisonInvalid(
            f"引用目标 {name}@{baseline_version}{path} 落入多个重命名祖先",
            details={
                "reason": "rename_ambiguous",
                "path": path,
                "ancestors": ancestors,
            },
        )

    if not registry.logical_path_exists(name, baseline_version, path):
        return BROKEN, _REASON_BASELINE_MISSING, None

    if ancestors:
        root = ancestors[0]
        suffix = ptr.parse(path)[len(ptr.parse(root)) :]
        mapped = ptr.format(list(ptr.parse(rename_map[root])) + list(suffix))
        if registry.logical_path_exists(name, candidate_version, mapped):
            return RENAMED, _REASON_RENAMED, mapped
        return BROKEN, _REASON_DELETED, None

    if registry.logical_path_exists(name, candidate_version, path):
        return READY, _REASON_UNCHANGED, path
    return BROKEN, _REASON_DELETED, None


def _sort_key(entry: dict[str, Any]) -> tuple:
    """来源稳定排序：先 Schema 来源（名称、版本、定位），后资产来源。"""
    src = entry["source"]
    if src["type"] == "schema":
        head: tuple = (
            0,
            src["schema"],
            src["version"],
            ptr.parse(src["path"]),
            src["path"],
        )
    else:
        head = (1, src["asset_id"])
    return head + (ptr.parse(entry["target_path"]), entry["target_path"])


def plan_reference_migration(
    registry: Registry,
    name: str,
    baseline_version: str,
    candidate_version: str,
    renames: Any,
) -> dict[str, Any]:
    """构造引用迁移规划（不触碰注册内容、报告库与检索索引）。"""
    _validate_identifiers(name, baseline_version, candidate_version)
    pairs = _parse_renames(renames)

    # 存在性校验放在结构校验之后：未知 Schema/版本抛 NotFoundError。
    baseline_fields = sf.expand(registry.get_schema(name, baseline_version).document)
    candidate_fields = sf.expand(
        registry.get_schema(name, candidate_version).document
    )
    _check_rename_fields(pairs, baseline_fields, candidate_fields)

    rename_map = dict(pairs)
    rename_froms = [src for src, _ in pairs]

    # 来源一：已注册 Schema 中指向基线版本的跨 Schema $ref（按当前文档
    # 重新逻辑化，与引用书写顺序无关）。
    schema_refs: list[tuple[str, str, str, str]] = []
    edge_count = 0
    for (src_name, src_version), edges in _build_edges(registry).items():
        for edge in edges:
            if edge.dst_schema == name and edge.dst_version == baseline_version:
                edge_count += 1
                schema_refs.append(
                    (src_name, src_version, edge.src_path, edge.dst_path)
                )
    if edge_count > limits.MAX_IMPACT_VISITED:
        raise ImpactAnalysisTooLarge(
            "引用边数量超过公开限制",
            details={"reason": "edges_exceeded", "limit": limits.MAX_IMPACT_VISITED},
        )

    # 来源二：资产 refs 中对基线版本字段的直接引用。
    asset_refs: list[tuple[str, str]] = []
    source_asset_ids: set[str] = set()
    for asset in registry.all_assets():
        for ref in asset.refs:
            if ref.schema == name and ref.version == baseline_version:
                asset_refs.append((asset.id, ref.path))
                source_asset_ids.add(asset.id)
    if len(source_asset_ids) > limits.MAX_IMPACT_ASSETS:
        raise ImpactAnalysisTooLarge(
            "来源资产数量超过公开限制",
            details={"reason": "assets_exceeded", "limit": limits.MAX_IMPACT_ASSETS},
        )

    # 按定位去重：同一逻辑来源定位的重复引用只保留一条；不同来源不合并。
    entries: dict[tuple, dict[str, Any]] = {}

    def add(identity: tuple, source: dict[str, Any], target_path: str) -> None:
        if identity in entries:
            return
        status, reason, suggested = _plan_target(
            registry,
            name,
            baseline_version,
            candidate_version,
            target_path,
            rename_froms,
            rename_map,
        )
        entries[identity] = {
            "source": source,
            "target_schema": name,
            "target_path": target_path,
            "suggested_version": candidate_version if suggested is not None else None,
            "suggested_path": suggested,
            "status": status,
            "reason": reason,
        }

    for src_name, src_version, src_path, target_path in schema_refs:
        add(
            ("schema", src_name, src_version, src_path, target_path),
            {
                "type": "schema",
                "schema": src_name,
                "version": src_version,
                "path": src_path,
            },
            target_path,
        )
    for asset_id, target_path in asset_refs:
        add(
            ("asset", asset_id, target_path),
            {"type": "asset", "asset_id": asset_id},
            target_path,
        )

    references = sorted(entries.values(), key=_sort_key)

    counts = {READY: 0, RENAMED: 0, BROKEN: 0}
    source_schemas: set[tuple[str, str]] = set()
    source_assets: set[str] = set()
    for entry in references:
        counts[entry["status"]] += 1
        src = entry["source"]
        if src["type"] == "schema":
            source_schemas.add((src["schema"], src["version"]))
        else:
            source_assets.add(src["asset_id"])

    return {
        "schema": name,
        "baseline_version": baseline_version,
        "candidate_version": candidate_version,
        "renames": [{"from": s, "to": d} for s, d in sorted(pairs)],
        "summary": {
            "total": len(references),
            "source_schemas": len(source_schemas),
            "source_assets": len(source_assets),
            "ready": counts[READY],
            "renamed": counts[RENAMED],
            "broken": counts[BROKEN],
        },
        "references": references,
    }
