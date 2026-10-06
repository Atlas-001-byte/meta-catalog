"""只读引用迁移规划（``plan_reference_migration``）。

同名 Schema 切换版本（基线版本 -> 候选版本，均已注册）时，盘点指向基线版本
的全部引用并给出迁移建议：

  * 跨 Schema ``$ref``：已注册 Schema 文档中指向 ``name@baseline_version``
    的外部引用边（按当前注册文档重新提取与逻辑化，与书写/注册顺序无关）；
  * 资产直接引用：资产 ``refs`` 中 ``(name, baseline_version)`` 的字段引用。

每条引用给出迁移状态（``status``）与唯一原因（``reason``）：

  * ``ready`` / ``path_unchanged``        目标未变且候选版本中存在；
  * ``renamed`` / ``path_renamed``        显式映射或重命名子树改变目标
                                          （深层引用保留后缀）；
  * ``broken`` / ``target_deleted``       基线字段存在，候选版本删除且无有效
                                          映射；
  * ``broken`` / ``baseline_target_missing`` 引用未解析到基线版本字段。

字段存在性口径与注册/影响分析一致（沿跨 Schema ``$ref`` 跳转解析）；重命名
映射沿用 :func:`meta_catalog.compare.validate_renames` 的全部校验语义。
不同来源的引用不合并；引用按来源定位去重并稳定排序。本模块只读注册簿：
不注册资源、不生成报告、不写入检索索引，相同输入返回相同结构。
"""

from __future__ import annotations

from typing import Any

from . import compare as compare_mod
from . import limits, pointer as ptr, schema_fields as sf
from .errors import ImpactAnalysisTooLarge, SchemaComparisonInvalid
from .registry import Registry, extract_ref_edges

READY = "ready"
RENAMED = "renamed"
BROKEN = "broken"

PATH_UNCHANGED = "path_unchanged"
PATH_RENAMED = "path_renamed"
TARGET_DELETED = "target_deleted"
BASELINE_TARGET_MISSING = "baseline_target_missing"

_SCHEMA_SOURCE = "schema"
_ASSET_SOURCE = "asset"


def _validate_inputs(
    name: Any, baseline_version: Any, candidate_version: Any, renames: Any
) -> None:
    """请求级结构校验（不依赖注册内容），失败抛 SchemaComparisonInvalid。"""
    if not isinstance(name, str) or not name:
        raise SchemaComparisonInvalid(
            "Schema 名称必须是非空字符串", details={"reason": "invalid_schema"}
        )
    for label, version in (("baseline_version", baseline_version), ("candidate_version", candidate_version)):
        if not isinstance(version, str) or not version:
            raise SchemaComparisonInvalid(
                f"{label} 必须是非空字符串",
                details={"reason": "invalid_version", label: version},
            )
    if baseline_version == candidate_version:
        raise SchemaComparisonInvalid(
            "基线版本与候选版本相同，无需迁移规划",
            details={"reason": "same_version", "version": baseline_version},
        )
    if renames is not None and not isinstance(renames, list):
        raise SchemaComparisonInvalid(
            "重命名映射必须是 [{from, to}, ...] 列表",
            details={"reason": "renames_not_list"},
        )


def _validate_renames(
    renames: list | None, old_fields: dict[str, Any], new_fields: dict[str, Any]
) -> list[tuple[str, str]]:
    """在 compare 的重命名语义之上，先校验映射路径是合法逻辑 JSONPointer。"""
    if not isinstance(renames, list):
        return compare_mod.validate_renames(renames, old_fields, new_fields)
    for i, item in enumerate(renames):
        if isinstance(item, dict):
            endpoints = (item.get("from"), item.get("to"))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            endpoints = (item[0], item[1])
        else:
            endpoints = ()
        for path in endpoints:
            if not isinstance(path, str):
                continue  # 结构问题交由 validate_renames 统一报告
            try:
                ptr.parse(path)
            except ValueError as exc:
                raise SchemaComparisonInvalid(
                    f"重命名路径不是合法逻辑 JSONPointer: {path!r}",
                    details={"reason": "invalid_pointer", "index": i, "path": path},
                ) from exc
    return compare_mod.validate_renames(renames, old_fields, new_fields)


def _schema_references(
    registry: Registry, name: str, baseline_version: str
) -> list[tuple[str, str, str, str]]:
    """指向基线版本的跨 Schema 引用：(源名, 源版本, 源逻辑路径, 目标逻辑路径)。

    从当前注册文档重新提取并按基线文档解析目标逻辑路径，结果与注册顺序无关；
    按定位四元组去重。
    """
    baseline_doc = registry.get_schema(name, baseline_version).document
    out: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for src_name, src_version in registry.registered_pairs():
        src_doc = registry.get_schema(src_name, src_version).document
        for raw in extract_ref_edges(src_name, src_version, src_doc):
            if raw.dst_schema != name or raw.dst_version != baseline_version:
                continue
            src_logical = ptr.resolve_logical(src_doc, raw.src_path) or (
                ptr.normalize_field_pointer(raw.src_path)
            )
            dst_logical = ptr.resolve_logical(baseline_doc, raw.dst_path) or (
                ptr.normalize_field_pointer(raw.dst_path)
            )
            key = (src_name, src_version, src_logical, dst_logical)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    if len(out) > limits.MAX_IMPACT_VISITED:
        raise ImpactAnalysisTooLarge(
            "引用边数量超过公开限制",
            details={"reason": "edges_exceeded", "limit": limits.MAX_IMPACT_VISITED},
        )
    return out


def _asset_references(
    registry: Registry, name: str, baseline_version: str
) -> list[tuple[str, str]]:
    """资产对基线版本字段的直接引用：(资产标识, 目标逻辑路径)。"""
    out: list[tuple[str, str]] = []
    assets: set[str] = set()
    for asset in registry.all_assets():
        for ref in asset.refs:
            if ref.schema == name and ref.version == baseline_version:
                out.append((asset.id, ref.path))
                assets.add(asset.id)
    if len(assets) > limits.MAX_IMPACT_ASSETS:
        raise ImpactAnalysisTooLarge(
            "引用资产数量超过公开限制",
            details={"reason": "assets_exceeded", "limit": limits.MAX_IMPACT_ASSETS},
        )
    return out


def _classify(
    registry: Registry,
    name: str,
    baseline_version: str,
    candidate_version: str,
    pairs: list[tuple[str, str]],
    target_path: str,
) -> tuple[str, str, str | None]:
    """判定单条引用的 (status, reason, 建议目标路径)。"""
    if not registry.logical_path_exists(name, baseline_version, target_path):
        return BROKEN, BASELINE_TARGET_MISSING, None

    rename_dsts = dict(pairs)
    ancestors = [src for src, _ in pairs if ptr.is_under(src, target_path)]
    if len(ancestors) > 1:
        raise SchemaComparisonInvalid(
            f"引用 {target_path} 落入多个重命名祖先，映射不明确",
            details={"reason": "rename_ancestor_ambiguous", "path": target_path},
        )
    if ancestors:
        root = ancestors[0]
        suffix = ptr.parse(target_path)[len(ptr.parse(root)) :]
        mapped = ptr.format(list(ptr.parse(rename_dsts[root])) + list(suffix))
        if registry.logical_path_exists(name, candidate_version, mapped):
            return RENAMED, PATH_RENAMED, mapped
        return BROKEN, TARGET_DELETED, None

    if registry.logical_path_exists(name, candidate_version, target_path):
        return READY, PATH_UNCHANGED, target_path
    return BROKEN, TARGET_DELETED, None


def plan_reference_migration(
    registry: Registry,
    name: Any,
    baseline_version: Any,
    candidate_version: Any,
    renames: list | None = None,
) -> dict[str, Any]:
    """规划同名 Schema 切换版本时跨 Schema 引用与资产直接引用的迁移（只读）。"""
    _validate_inputs(name, baseline_version, candidate_version, renames)

    baseline = registry.get_schema(name, baseline_version)  # 不存在抛 NotFoundError
    candidate = registry.get_schema(name, candidate_version)
    old_fields = sf.expand(baseline.document)
    new_fields = sf.expand(candidate.document)
    pairs = _validate_renames(renames, old_fields, new_fields)

    edge_refs = _schema_references(registry, name, baseline_version)
    asset_refs = _asset_references(registry, name, baseline_version)

    references: list[dict[str, Any]] = []
    counts = {READY: 0, RENAMED: 0, BROKEN: 0}

    def add_entry(
        source_type: str,
        source_schema: str | None,
        source_version: str | None,
        source_path: str | None,
        asset_id: str | None,
        target_path: str,
    ) -> None:
        status, reason, suggested_path = _classify(
            registry, name, baseline_version, candidate_version, pairs, target_path
        )
        counts[status] += 1
        references.append(
            {
                "source_type": source_type,
                "source_schema": source_schema,
                "source_version": source_version,
                "source_path": source_path,
                "asset_id": asset_id,
                "target_schema": name,
                "target_path": target_path,
                "suggested_version": (
                    candidate_version if status != BROKEN else None
                ),
                "suggested_path": suggested_path,
                "status": status,
                "reason": reason,
            }
        )

    for src_name, src_version, src_path, dst_path in edge_refs:
        add_entry(_SCHEMA_SOURCE, src_name, src_version, src_path, None, dst_path)
    for asset_id, path in asset_refs:
        add_entry(_ASSET_SOURCE, None, None, None, asset_id, path)

    # 按来源稳定排序：跨 Schema 引用在前（按来源定位与目标路径），资产引用
    # 在后（按资产标识与目标路径）；不同来源的引用不合并。
    def sort_key(entry: dict[str, Any]) -> tuple:
        if entry["source_type"] == _SCHEMA_SOURCE:
            return (
                0,
                entry["source_schema"],
                entry["source_version"],
                ptr.parse(entry["source_path"]),
                entry["source_path"],
                ptr.parse(entry["target_path"]),
                entry["target_path"],
            )
        return (
            1,
            entry["asset_id"],
            ptr.parse(entry["target_path"]),
            entry["target_path"],
        )

    references.sort(key=sort_key)

    source_schemas = {
        (e["source_schema"], e["source_version"])
        for e in references
        if e["source_type"] == _SCHEMA_SOURCE
    }
    source_assets = {
        e["asset_id"] for e in references if e["source_type"] == _ASSET_SOURCE
    }

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
