"""升级影响汇总。

在字段级变更报告之上，按资产汇总一次 Schema 升级的影响：复用
:func:`meta_catalog.compare.build_report` 的全部比较语义（候选解析、字段对齐、
重命名、稳定排序与报告标识），不产生新的去重口径。

每个资产对每条命中变更保留至多一条记录，命中方式 ``impact_kind``：

  * ``direct``     资产直接引用了变更字段（出现在变更的直接影响资产中）；
  * ``transitive`` 资产经其他 Schema 传递引用了变更字段；
  * ``both``       同一变更上直接与传递同时命中。

资产状态 ``status`` 按命中变更的最严重兼容结论取值，严重顺序为
``breaking`` > ``compatible`` > ``metadata``；没有任何命中时为 ``unaffected``。
汇总只读注册簿，不构造报告、不入检索索引、不改动任何注册内容。
"""

from __future__ import annotations

from typing import Any

from . import compare as compare_mod
from .errors import NotFoundError
from .registry import Registry

# status 严重顺序：下标越小越严重。
_SEVERITY = ("breaking", "compatible", "metadata")
_SEVERITY_RANK = {s: i for i, s in enumerate(_SEVERITY)}


def _resolve_asset_ids(
    registry: Registry, asset_ids: list[str] | None
) -> list[str]:
    """校验并去重资产选择，返回按 asset_id 升序的入选清单。

    ``None`` 表示覆盖全部资产；未知资产抛 :class:`NotFoundError`。
    """
    if asset_ids is None:
        return [a.id for a in registry.all_assets()]
    unique: set[str] = set()
    for aid in asset_ids:
        if not isinstance(aid, str) or not aid:
            raise NotFoundError(
                f"资产 {aid!r} 不存在", details={"asset": aid}
            )
        if aid in unique:
            continue
        registry.get_asset(aid)  # 未知资产抛 NotFoundError
        unique.add(aid)
    return sorted(unique)


def build_upgrade_report(
    registry: Registry,
    name: str,
    baseline_version: str,
    candidate_document: Any,
    candidate_version: str | None,
    renames: list | None,
    asset_ids: list[str] | None,
) -> dict[str, Any]:
    """构造升级影响汇总（不触碰注册内容、报告库与检索索引）。"""
    # 比较语义与 compare_schemas 完全一致：内部做字段数/变更数/影响链限制校验。
    report = compare_mod.build_report(
        registry,
        name,
        baseline_version,
        candidate_document,
        candidate_version,
        renames,
    )

    # 比较输入校验通过后再校验资产选择（未知资产抛 NotFoundError）。
    selected = _resolve_asset_ids(registry, asset_ids)

    assets = {a.id: a for a in registry.all_assets()}

    # 每个入选资产收集 (变更索引, impact_kind)；同一变更只保留一条。
    hits: dict[str, dict[int, str]] = {aid: {} for aid in selected}
    hit_paths: set[str] = set()
    for i, change in enumerate(report["changes"]):
        direct_ids = {a["asset_id"] for a in change["direct_assets"]}
        transitive_ids = {a["asset_id"] for a in change["transitive_assets"]}
        for aid in selected:
            is_direct = aid in direct_ids
            is_transitive = aid in transitive_ids
            if not is_direct and not is_transitive:
                continue
            impact_kind = (
                "both" if is_direct and is_transitive
                else "direct" if is_direct
                else "transitive"
            )
            hits[aid][i] = impact_kind
            hit_paths.add(change["path"])

    asset_entries: list[dict[str, Any]] = []
    counts = {s: 0 for s in _SEVERITY}
    for aid in selected:
        asset = assets[aid]
        per_change = hits[aid]
        if not per_change:
            asset_entries.append(
                {
                    "asset_id": aid,
                    "name": asset.name,
                    "kind": asset.kind,
                    "status": "unaffected",
                    "changes": [],
                }
            )
            continue

        status = ""
        rank = len(_SEVERITY)
        changes_out: list[dict[str, Any]] = []
        # 变更沿用报告的稳定排序：按报告中的索引升序遍历。
        for i, change in enumerate(report["changes"]):
            impact_kind = per_change.get(i)
            if impact_kind is None:
                continue
            entry = {k: change[k] for k in change}
            entry["impact_kind"] = impact_kind
            changes_out.append(entry)
            comp = change["compatibility"]
            comp_rank = _SEVERITY_RANK.get(comp, len(_SEVERITY))
            if comp_rank < rank:
                rank = comp_rank
                status = comp

        counts[status] += 1
        asset_entries.append(
            {
                "asset_id": aid,
                "name": asset.name,
                "kind": asset.kind,
                "status": status,
                "changes": changes_out,
            }
        )

    unaffected = len(selected) - sum(counts.values())
    return {
        "report_id": report["report_id"],
        "schema": report["schema"],
        "baseline_version": report["baseline_version"],
        "candidate_version": report["candidate_version"],
        "summary": {
            "asset_total": len(selected),
            "breaking_assets": counts["breaking"],
            "compatible_assets": counts["compatible"],
            "metadata_assets": counts["metadata"],
            "unaffected_assets": unaffected,
            "changed_paths": len(hit_paths),
        },
        "assets": asset_entries,
    }
