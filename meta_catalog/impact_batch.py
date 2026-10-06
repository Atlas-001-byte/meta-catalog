"""按字段集合的批量影响分析（``analyze_impact_batch``）。

在单字段影响分析（:func:`meta_catalog.impact.analyze_field`）的命中口径之上，
按一组逻辑字段筛选受影响资产：

  * ``any`` 模式：资产至少命中一个字段即入选；
  * ``all`` 模式：资产必须命中每一个字段才入选。

字段按首次出现顺序去重；可选资产标识列表去重后限定候选范围，缺省覆盖全部
资产，空列表不选任何资产。每个入选资产给出 ``matched_fields``（按输入顺序
的实际命中字段）与逐字段明细 ``fields``：命中方式（``direct`` /
``transitive`` / ``both``）、与单字段分析同口径的直接信息与传递引用证据
（证据按稳定路径去重排序，根字段与父子字段沿用既有包含关系）。

本模块只读注册簿：不注册资源、不生成报告、不写入检索索引；相同输入返回
相同结构，返回值不与注册内容共享可变引用。
"""

from __future__ import annotations

from typing import Any

from . import limits
from . import pointer as ptr
from .errors import (
    ImpactAnalysisInvalid,
    ImpactAnalysisTooLarge,
    NotFoundError,
)
from .impact import analyze_field
from .registry import Registry

_MODES = ("all", "any")


def analyze_impact_batch(registry: Registry, request: Any) -> dict[str, Any]:
    """按字段集合筛选受影响资产（只读，不改动任何注册内容）。"""
    schema, version, paths, mode, candidate_ids = _validate_request(registry, request)

    # 空候选（asset_ids=[]）直接返回空资产集合，不做任何链式解析。
    if not candidate_ids:
        return _result(
            schema, version, paths, mode,
            0, {p: set() for p in paths}, set(), [],
        )

    candidate_set = set(candidate_ids)

    # 逐字段做单字段影响分析（与 analyze_impact 完全同口径，含链深、引用边
    # 与单字段影响资产数限制），再把命中限定到候选范围。
    direct_by_field: dict[str, dict[str, dict]] = {}
    transitive_by_field: dict[str, dict[str, list[dict]]] = {}
    hits_by_field: dict[str, set[str]] = {}
    for path in paths:
        impacts = analyze_field(registry, schema, version, path)
        direct = {
            a["asset_id"]: a
            for a in impacts["direct_assets"]
            if a["asset_id"] in candidate_set
        }
        transitive: dict[str, list[dict]] = {}
        for a in impacts["transitive_assets"]:
            if a["asset_id"] in candidate_set:
                transitive.setdefault(a["asset_id"], []).append(a)
        direct_by_field[path] = direct
        transitive_by_field[path] = transitive
        hits_by_field[path] = set(direct) | set(transitive)

    union_ids: set[str] = set()
    for path in paths:
        union_ids |= hits_by_field[path]
    if len(union_ids) > limits.MAX_IMPACT_ASSETS:
        raise ImpactAnalysisTooLarge(
            "入选资产数量超过公开限制",
            details={"reason": "assets_exceeded", "limit": limits.MAX_IMPACT_ASSETS},
        )

    if mode == "all":
        final_ids = set(hits_by_field[paths[0]])
        for path in paths[1:]:
            final_ids &= hits_by_field[path]
    else:
        final_ids = union_ids

    assets_by_id = {a.id: a for a in registry.all_assets()}
    entries: list[dict[str, Any]] = []
    for aid in sorted(final_ids):
        asset = assets_by_id[aid]
        matched = [p for p in paths if aid in hits_by_field[p]]
        fields_detail: dict[str, Any] = {}
        has_direct = False
        has_transitive = False
        for path in matched:
            direct = direct_by_field[path].get(aid)
            evidence = transitive_by_field[path].get(aid, [])
            is_direct = direct is not None
            is_transitive = bool(evidence)
            has_direct = has_direct or is_direct
            has_transitive = has_transitive or is_transitive
            fields_detail[path] = {
                "impact_kind": _impact_kind(is_direct, is_transitive),
                "direct": (
                    {
                        "asset_id": direct["asset_id"],
                        "name": direct["name"],
                        "kind": direct["kind"],
                    }
                    if is_direct
                    else None
                ),
                "transitive": [
                    {
                        "schema": e["schema"],
                        "version": e["version"],
                        "path": e["path"],
                        "matched_paths": list(e["matched_paths"]),
                    }
                    for e in evidence
                ],
            }
        entries.append(
            {
                "asset_id": asset.id,
                "name": asset.name,
                "kind": asset.kind,
                "matched_fields": matched,
                "fields": fields_detail,
                "impact_kind": _impact_kind(has_direct, has_transitive),
            }
        )

    return _result(
        schema, version, paths, mode,
        len(candidate_ids), hits_by_field, union_ids, entries,
    )


def _impact_kind(has_direct: bool, has_transitive: bool) -> str:
    if has_direct and has_transitive:
        return "both"
    return "direct" if has_direct else "transitive"


def _result(
    schema: str,
    version: str,
    paths: list[str],
    mode: str,
    candidate_count: int,
    hits_by_field: dict[str, set[str]],
    union_ids: set[str],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": schema,
        "version": version,
        "paths": list(paths),
        "mode": mode,
        "summary": {
            "candidate_assets": candidate_count,
            "selected_assets": len(union_ids),
            "field_hits": {p: len(hits_by_field[p]) for p in paths},
            "matched_assets": len(entries),
        },
        "assets": entries,
    }


def _validate_request(
    registry: Registry, request: Any
) -> tuple[str, str, list[str], str, list[str]]:
    """结构校验（ImpactAnalysisInvalid）与存在性校验（NotFoundError）。

    返回 ``(schema, version, 去重后路径, mode, 候选资产标识升序列表)``；
    校验失败不返回任何部分结果。
    """
    if not isinstance(request, dict):
        raise ImpactAnalysisInvalid(
            "请求必须是字典", details={"reason": "request_not_mapping"}
        )

    schema = request.get("schema")
    if not isinstance(schema, str) or not schema:
        raise ImpactAnalysisInvalid(
            "schema 必须是非空字符串", details={"reason": "invalid_schema"}
        )
    version = request.get("version")
    if not isinstance(version, str) or not version:
        raise ImpactAnalysisInvalid(
            "version 必须是非空字符串", details={"reason": "invalid_version"}
        )

    raw_paths = request.get("paths")
    if not isinstance(raw_paths, list) or any(
        not isinstance(p, str) for p in raw_paths
    ):
        raise ImpactAnalysisInvalid(
            "paths 必须是字符串列表", details={"reason": "invalid_paths"}
        )

    mode = request.get("mode")
    if mode not in _MODES:
        raise ImpactAnalysisInvalid(
            f"mode 必须是 all 或 any: {mode!r}",
            details={"reason": "invalid_mode", "mode": mode},
        )

    asset_ids = request.get("asset_ids")
    if asset_ids is not None and (
        not isinstance(asset_ids, list)
        or any(not isinstance(a, str) or not a for a in asset_ids)
    ):
        raise ImpactAnalysisInvalid(
            "asset_ids 必须是非空字符串列表",
            details={"reason": "invalid_asset_ids"},
        )

    paths: list[str] = []
    seen: set[str] = set()
    for path in raw_paths:
        try:
            ptr.parse(path)
        except ValueError as exc:
            raise ImpactAnalysisInvalid(
                f"路径不是合法逻辑 JSONPointer: {path!r}",
                details={"reason": "invalid_pointer", "path": path},
            ) from exc
        if path not in seen:
            seen.add(path)
            paths.append(path)
    if not paths:
        raise ImpactAnalysisInvalid(
            "去重后的字段集合为空", details={"reason": "empty_paths"}
        )

    # 存在性校验放在结构校验之后：未知 Schema/版本/字段抛 NotFoundError。
    registry.get_schema(schema, version)
    for path in paths:
        if not registry.logical_path_exists(schema, version, path):
            raise NotFoundError(
                f"字段 {schema}@{version}{path} 不可达",
                details={"schema": schema, "version": version, "path": path},
            )

    if asset_ids is None:
        candidates = [a.id for a in registry.all_assets()]
    else:
        candidates = sorted(set(asset_ids))
        for aid in candidates:
            registry.get_asset(aid)  # 未知资产抛 NotFoundError
    return schema, version, paths, mode, candidates
