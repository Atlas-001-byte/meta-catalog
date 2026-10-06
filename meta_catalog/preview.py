"""变更预检（``preview_changes``）。

对一批尚未提交的 Schema 变更做只读预检：不注册 Schema、不生成变更报告、
不写入检索索引，既有注册内容、报告库与检索行为完全不受影响。

批次元素形如::

    {"schema": "Person", "version": "1.0", "changeType": "add_field",
     "path": "/email", "definition": {"type": "string"}}

``changeType`` 仅限 ``add_field`` / ``remove_field`` / ``change_type`` /
``compatibility_check`` / ``unregister_schema``：

  * ``add_field`` / ``change_type`` 携带逻辑字段路径 ``path`` 与字段定义
    ``definition``；
  * ``remove_field`` 携带逻辑字段路径 ``path``；
  * ``compatibility_check`` 携带候选 Schema 文档 ``candidate``；
  * ``unregister_schema`` 仅由 ``schema`` + ``version`` 定位版本。

校验与错误口径：

  * 缺字段、未知 ``changeType``、非法逻辑 JSONPointer、同一路径重复出现但
    内容不匹配、字段定义/候选文档不是合法 JSON Schema，抛
    :class:`SchemaComparisonInvalid`；
  * 引用的 Schema 或版本未注册，抛 :class:`NotFoundError`；
  * 影响链深度、引用边数或影响资产数超过公开限制，抛
    :class:`ImpactAnalysisTooLarge`；
  * 批次内部矛盾（同一路径被不同变更类型覆盖、``add_field`` 落在已注册
    字段上、``unregister_schema`` 与同版本其他变更并存）或
    ``remove_field`` / ``change_type`` 指向未注册字段时，不抛错，返回
    ``accepted=False`` 与 ``conflicts``（``CHANGE_CONFLICT`` /
    ``FIELD_NOT_FOUND``），此时 ``impactedItems`` 与 ``searchPreview``
    为空集合。

兼容性判定沿用既有字段级比较（:func:`meta_catalog.schema_fields.classify_pair`
与 :func:`meta_catalog.compare.build_report`）；``compatibility_check`` 中
旧版本未定义的扩展字段不新增要求，一律按 ``compatible`` 计入预览。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from . import compare as compare_mod
from . import limits
from . import pointer as ptr
from . import schema_fields as sf
from .errors import ImpactAnalysisTooLarge, SchemaComparisonInvalid
from .impact import _build_outgoing, analyze_field, forward_reach
from .indexing import change_doc
from .registry import Registry
from .search import SearchIndex
from .validator import validate_schema

ADD_FIELD = "add_field"
REMOVE_FIELD = "remove_field"
CHANGE_TYPE = "change_type"
COMPATIBILITY_CHECK = "compatibility_check"
UNREGISTER_SCHEMA = "unregister_schema"

CHANGE_TYPES = (
    ADD_FIELD,
    REMOVE_FIELD,
    CHANGE_TYPE,
    COMPATIBILITY_CHECK,
    UNREGISTER_SCHEMA,
)

CHANGE_CONFLICT = "CHANGE_CONFLICT"
FIELD_NOT_FOUND = "FIELD_NOT_FOUND"

# search 参数沿用 MetaCatalog.search 的检索条件。
_SEARCH_KEYS = frozenset(
    {
        "keyword",
        "schema",
        "version",
        "field_path",
        "change_kind",
        "compatibility",
        "asset_name",
        "doc_type",
        "limit",
    }
)

_PATH_TYPES = (ADD_FIELD, REMOVE_FIELD, CHANGE_TYPE)


def preview_changes(
    registry: Registry,
    index: SearchIndex,
    changes: Any,
    search: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """预检一批变更，返回 ``accepted`` / ``conflicts`` / ``impactedItems`` /
    ``searchPreview``。只读：不触碰注册簿、报告库与检索索引。"""
    search_kwargs = _validate_search(search)
    normalized = _validate_batch(changes)

    empty_impacts: dict[str, list] = {"direct": [], "transitive": []}
    empty_preview: dict[str, list] = {"added": [], "removed": [], "replaced": []}
    if not normalized:
        return {
            "accepted": True,
            "conflicts": [],
            "impactedItems": empty_impacts,
            "searchPreview": empty_preview,
        }

    # 存在性：Schema/版本未注册抛 NotFoundError（在冲突判定之前）。
    for ch in normalized:
        registry.get_schema(ch["schema"], ch["version"])

    conflicts = _find_conflicts(registry, normalized)
    if conflicts:
        return {
            "accepted": False,
            "conflicts": conflicts,
            "impactedItems": empty_impacts,
            "searchPreview": empty_preview,
        }

    # compatibility_check 的比较报告只算一次，影响汇总与检索预览共用。
    reports = {
        ch["index"]: _compat_report(registry, ch)
        for ch in normalized
        if ch["changeType"] == COMPATIBILITY_CHECK
    }
    impacted = _collect_impacts(registry, normalized, reports)
    preview = _build_search_preview(registry, index, normalized, reports, search_kwargs)
    return {
        "accepted": True,
        "conflicts": [],
        "impactedItems": impacted,
        "searchPreview": preview,
    }


# ---------------------------------------------------------------- 输入校验
def _validate_search(search: Any) -> dict[str, Any] | None:
    """校验 search 条件；返回可传给 ``SearchIndex.search`` 的 kwargs。"""
    if search is None:
        return None
    if not isinstance(search, dict):
        raise SchemaComparisonInvalid(
            "search 必须是检索条件字典",
            details={"reason": "search_not_mapping"},
        )
    unknown = sorted(k for k in search if k not in _SEARCH_KEYS)
    if unknown:
        raise SchemaComparisonInvalid(
            f"search 含未知检索条件: {unknown}",
            details={"reason": "search_unknown_filter", "filters": unknown},
        )
    return dict(search)


def _validate_batch(changes: Any) -> list[dict[str, Any]]:
    """结构校验与规范化；失败抛 :class:`SchemaComparisonInvalid`。

    返回按原始顺序的规范化变更列表（完全相同的重复条目已去重，
    ``index`` 为首次出现的位置）。
    """
    if not isinstance(changes, list):
        raise SchemaComparisonInvalid(
            "changes 必须是 JSON 列表",
            details={"reason": "changes_not_list"},
        )

    normalized: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    content_by_target: dict[tuple, str] = {}
    for i, raw in enumerate(changes):
        if not isinstance(raw, dict):
            raise SchemaComparisonInvalid(
                f"第 {i} 条变更必须是对象",
                details={"reason": "change_not_object", "index": i},
            )
        ch = _normalize_change(raw, i)
        content = _canonical_content(ch)

        if ch["changeType"] in _PATH_TYPES:
            target = (ch["schema"], ch["version"], ch["changeType"], ch["path"])
            # 重复路径内容不匹配：请求本身非法。
            if target in content_by_target and content_by_target[target] != content:
                raise SchemaComparisonInvalid(
                    f"第 {i} 条变更与之前同路径条目的内容不匹配",
                    details={
                        "reason": "duplicate_content_mismatch",
                        "index": i,
                        "schema": ch["schema"],
                        "version": ch["version"],
                        "changeType": ch["changeType"],
                        "path": ch["path"],
                    },
                )
            content_by_target.setdefault(target, content)
            dedup_key = (target, content)
        else:
            # compatibility_check / unregister_schema：完全相同才去重，
            # 不同候选文档的兼容性检查相互独立。
            dedup_key = (
                ch["schema"],
                ch["version"],
                ch["changeType"],
                content,
            )
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        normalized.append(ch)
    return normalized


def _normalize_change(raw: dict, index: int) -> dict[str, Any]:
    def missing(name: str) -> SchemaComparisonInvalid:
        return SchemaComparisonInvalid(
            f"第 {index} 条变更缺少字段 {name!r}",
            details={"reason": "missing_field", "index": index, "field": name},
        )

    for key in ("schema", "version", "changeType"):
        if key not in raw:
            raise missing(key)
    schema, version, ctype = raw["schema"], raw["version"], raw["changeType"]
    if not isinstance(schema, str) or not schema:
        raise SchemaComparisonInvalid(
            f"第 {index} 条变更的 schema 必须是非空字符串",
            details={"reason": "invalid_schema", "index": index},
        )
    if not isinstance(version, str) or not version:
        raise SchemaComparisonInvalid(
            f"第 {index} 条变更的 version 必须是非空字符串",
            details={"reason": "invalid_version", "index": index},
        )
    if ctype not in CHANGE_TYPES:
        raise SchemaComparisonInvalid(
            f"第 {index} 条变更的 changeType 未知: {ctype!r}",
            details={
                "reason": "unknown_change_type",
                "index": index,
                "changeType": ctype,
            },
        )

    ch: dict[str, Any] = {
        "index": index,
        "schema": schema,
        "version": version,
        "changeType": ctype,
    }
    if ctype in _PATH_TYPES:
        ch["path"] = _require_path(raw, index)
    if ctype in (ADD_FIELD, CHANGE_TYPE):
        if "definition" not in raw:
            raise missing("definition")
        validate_schema(raw["definition"])
        ch["definition"] = copy.deepcopy(raw["definition"])
    if ctype == COMPATIBILITY_CHECK:
        if "candidate" not in raw:
            raise missing("candidate")
        validate_schema(raw["candidate"])
        ch["candidate"] = copy.deepcopy(raw["candidate"])
    return ch


def _require_path(raw: dict, index: int) -> str:
    if "path" not in raw:
        raise SchemaComparisonInvalid(
            f"第 {index} 条变更缺少字段 'path'",
            details={"reason": "missing_field", "index": index, "field": "path"},
        )
    path = raw["path"]
    try:
        if not isinstance(path, str):
            raise ValueError
        ptr.parse(path)
    except ValueError as exc:
        raise SchemaComparisonInvalid(
            f"第 {index} 条变更的 path 不是合法逻辑 JSONPointer: {path!r}",
            details={"reason": "invalid_pointer", "index": index, "path": path},
        ) from exc
    return path


def _canonical_content(ch: dict[str, Any]) -> str:
    """用于重复检测的内容指纹（不含定位字段）。"""
    parts: list[Any] = []
    if "definition" in ch:
        parts.append(ch["definition"])
    if "candidate" in ch:
        parts.append(ch["candidate"])
    return json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- 冲突检测
def _find_conflicts(
    registry: Registry, changes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """收集批次矛盾与未注册字段引用（不抛错，全部列出）。"""
    conflicts: list[dict[str, Any]] = []
    fields_cache: dict[tuple[str, str], dict[str, sf.Field]] = {}

    def fields_of(schema: str, version: str) -> dict[str, sf.Field]:
        key = (schema, version)
        if key not in fields_cache:
            fields_cache[key] = sf.expand(registry.get_schema(schema, version).document)
        return fields_cache[key]

    def resource(ch: dict[str, Any]) -> dict[str, Any]:
        res: dict[str, Any] = {
            "index": ch["index"],
            "schema": ch["schema"],
            "version": ch["version"],
        }
        if "path" in ch:
            res["path"] = ch["path"]
        return res

    # 1) remove_field / change_type 必须指向已注册字段；add_field 不得落在
    #    已注册字段上。
    for ch in changes:
        if ch["changeType"] not in _PATH_TYPES:
            continue
        exists = ch["path"] in fields_of(ch["schema"], ch["version"])
        if ch["changeType"] in (REMOVE_FIELD, CHANGE_TYPE) and not exists:
            conflicts.append(
                {
                    "code": FIELD_NOT_FOUND,
                    "index": ch["index"],
                    "resources": [resource(ch)],
                }
            )
        elif ch["changeType"] == ADD_FIELD and exists:
            conflicts.append(
                {
                    "code": CHANGE_CONFLICT,
                    "index": ch["index"],
                    "resources": [resource(ch)],
                }
            )

    # 2) 同一路径被不同变更类型覆盖：批次矛盾（在较晚的条目上报）。
    by_path: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for ch in changes:
        if ch["changeType"] in _PATH_TYPES:
            by_path.setdefault((ch["schema"], ch["version"], ch["path"]), []).append(ch)
    for group in by_path.values():
        if len({ch["changeType"] for ch in group}) <= 1:
            continue
        group.sort(key=lambda ch: ch["index"])
        resources = [resource(ch) for ch in group]
        for ch in group[1:]:
            conflicts.append(
                {"code": CHANGE_CONFLICT, "index": ch["index"], "resources": resources}
            )

    # 3) unregister_schema 与同版本任何其他变更并存：批次矛盾。
    by_version: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ch in changes:
        by_version.setdefault((ch["schema"], ch["version"]), []).append(ch)
    for group in by_version.values():
        unreg = [ch for ch in group if ch["changeType"] == UNREGISTER_SCHEMA]
        others = [ch for ch in group if ch["changeType"] != UNREGISTER_SCHEMA]
        if not unreg or not others:
            continue
        combined = sorted(unreg + others, key=lambda ch: ch["index"])
        resources = [resource(ch) for ch in combined]
        for ch in combined[1:]:
            conflicts.append(
                {"code": CHANGE_CONFLICT, "index": ch["index"], "resources": resources}
            )

    # 同一 (code, index) 可能因多条规则重复出现，去重后按 (index, code) 排序。
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for c in sorted(conflicts, key=lambda c: (c["index"], c["code"])):
        key = (c["code"], c["index"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ---------------------------------------------------------------- 影响汇总
def _collect_impacts(
    registry: Registry,
    changes: list[dict[str, Any]],
    reports: dict[int, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    direct: list[dict[str, Any]] = []
    transitive: list[dict[str, Any]] = []

    def add_impacts(
        ch: dict[str, Any], path: str, impacts: dict, compatibility: str
    ) -> None:
        source = {"index": ch["index"], "changeType": ch["changeType"]}
        for a in impacts["direct_assets"]:
            direct.append(
                {
                    "asset_id": a["asset_id"],
                    "name": a["name"],
                    "kind": a["kind"],
                    "schema": ch["schema"],
                    "version": ch["version"],
                    "path": path,
                    "compatibility": compatibility,
                    "source": source,
                }
            )
        for a in impacts["transitive_assets"]:
            transitive.append(
                {
                    "asset_id": a["asset_id"],
                    "name": a["name"],
                    "kind": a["kind"],
                    "schema": a["schema"],
                    "version": a["version"],
                    "path": a["path"],
                    "matched_paths": list(a["matched_paths"]),
                    "compatibility": compatibility,
                    "source": source,
                }
            )

    for ch in changes:
        ctype = ch["changeType"]
        if ctype == ADD_FIELD:
            continue  # 新字段尚无资产引用，无影响
        if ctype in (REMOVE_FIELD, CHANGE_TYPE):
            impacts = analyze_field(registry, ch["schema"], ch["version"], ch["path"])
            if ctype == REMOVE_FIELD:
                compatibility = compare_mod.BREAKING
            else:
                old = sf.expand(
                    registry.get_schema(ch["schema"], ch["version"]).document
                )[ch["path"]].schema
                compatibility = sf.classify_pair(old, ch["definition"])
            add_impacts(ch, ch["path"], impacts, compatibility)
        elif ctype == COMPATIBILITY_CHECK:
            for change in reports[ch["index"]]["changes"]:
                add_impacts(
                    ch,
                    change["path"],
                    {
                        "direct_assets": change["direct_assets"],
                        "transitive_assets": change["transitive_assets"],
                    },
                    change["compatibility"],
                )
        elif ctype == UNREGISTER_SCHEMA:
            impacts = _unregister_impacts(registry, ch["schema"], ch["version"])
            add_impacts(ch, "", impacts, compare_mod.BREAKING)

    def impact_key(item: dict[str, Any]) -> tuple:
        return (
            item["schema"],
            item["version"],
            item["asset_id"],
            item["path"],
            item["source"]["index"],
        )

    direct.sort(key=impact_key)
    transitive.sort(key=impact_key)
    return {"direct": direct, "transitive": transitive}


def _compat_report(registry: Registry, ch: dict[str, Any]) -> dict[str, Any]:
    """compatibility_check 的字段级比较报告（只读，不入库）。

    旧版本未定义的扩展字段不新增要求：``added`` 条目一律按
    ``compatible`` 计入预览。
    """
    report = compare_mod.build_report(
        registry,
        ch["schema"],
        ch["version"],
        copy.deepcopy(ch["candidate"]),
        None,
        None,
    )
    for change in report["changes"]:
        if change["change_kind"] == compare_mod.ADDED:
            change["compatibility"] = compare_mod.COMPATIBLE
    return report


def _unregister_impacts(registry: Registry, schema: str, version: str) -> dict[str, Any]:
    """注销整个版本的影响：直接引用该版本的资产 + 经其他 Schema 传递到达的。"""
    outgoing = _build_outgoing(registry)
    cache: dict[tuple[str, str, str], list] = {}
    direct: dict[str, dict[str, Any]] = {}
    transitive: list[dict[str, Any]] = []
    for asset in registry.all_assets():
        for ref in asset.refs:
            if ref.schema == schema and ref.version == version:
                direct.setdefault(
                    asset.id,
                    {"asset_id": asset.id, "name": asset.name, "kind": asset.kind},
                )
                continue
            reached = forward_reach(
                registry, ref.schema, ref.version, ref.path, outgoing, cache
            )
            matched = sorted(
                {r.path for r in reached if r.schema == schema and r.version == version},
                key=lambda p: (ptr.parse(p), p),
            )
            if matched:
                transitive.append(
                    {
                        "asset_id": asset.id,
                        "name": asset.name,
                        "kind": asset.kind,
                        "schema": ref.schema,
                        "version": ref.version,
                        "path": ref.path,
                        "matched_paths": matched,
                    }
                )
    if len(direct) + len(transitive) > limits.MAX_IMPACT_ASSETS:
        raise ImpactAnalysisTooLarge(
            "影响资产数量超过公开限制",
            details={"reason": "assets_exceeded", "limit": limits.MAX_IMPACT_ASSETS},
        )
    return {
        "direct_assets": [direct[k] for k in sorted(direct)],
        "transitive_assets": transitive,
    }


# ---------------------------------------------------------------- 检索预览
def _build_search_preview(
    registry: Registry,
    index: SearchIndex,
    changes: list[dict[str, Any]],
    reports: dict[int, dict[str, Any]],
    search_kwargs: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    """预测索引层面的新增/移除/替换文档（可选地按 search 条件过滤）。"""
    replaced: dict[tuple[str, str], dict[str, Any]] = {}
    removed: dict[tuple[str, str], dict[str, Any]] = {}
    added: list[dict[str, Any]] = []

    def schema_entry(ch: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f"schema:{ch['schema']}@{ch['version']}",
            "type": "schema",
            "schema": ch["schema"],
            "version": ch["version"],
            "report_id": None,
            "path": None,
            "index": ch["index"],
            "reason": ch["changeType"],
        }

    for ch in changes:
        ctype = ch["changeType"]
        key = (ch["schema"], ch["version"])
        if ctype in (ADD_FIELD, REMOVE_FIELD, CHANGE_TYPE):
            if key not in removed and key not in replaced:
                replaced[key] = schema_entry(ch)
        elif ctype == UNREGISTER_SCHEMA:
            replaced.pop(key, None)
            removed[key] = schema_entry(ch)
        elif ctype == COMPATIBILITY_CHECK:
            report = reports[ch["index"]]
            for seq, change in enumerate(report["changes"]):
                added.append(
                    {
                        "id": f"change:{report['report_id']}:{seq}",
                        "type": "change",
                        "schema": report["schema"],
                        "version": report["baseline_version"],
                        "report_id": report["report_id"],
                        "path": change["path"],
                        "index": ch["index"],
                        "reason": COMPATIBILITY_CHECK,
                    }
                )

    if search_kwargs is not None:
        schema_hits = {
            (h["name"], h["version"])
            for h in index.search(**search_kwargs)
            if h["type"] == "schema"
        }
        replaced = {k: v for k, v in replaced.items() if k in schema_hits}
        removed = {k: v for k, v in removed.items() if k in schema_hits}
        added = _filter_added_by_search(reports, added, search_kwargs)

    def preview_key(item: dict[str, Any]) -> tuple:
        return (
            item["schema"] or "",
            item["version"] or "",
            item["report_id"] or "",
            item["path"] or "",
            item["index"],
        )

    return {
        "added": sorted(added, key=preview_key),
        "removed": sorted(removed.values(), key=preview_key),
        "replaced": sorted(replaced.values(), key=preview_key),
    }


def _filter_added_by_search(
    reports: dict[int, dict[str, Any]],
    added: list[dict[str, Any]],
    search_kwargs: dict[str, Any],
) -> list[dict[str, Any]]:
    """在临时索引上模拟新增变更文档的检索命中（不写入真实索引）。"""
    if not added:
        return added
    temp = SearchIndex()
    for entry in added:
        report = reports[entry["index"]]
        seq = int(entry["id"].rsplit(":", 1)[1])
        key, text_fields, payload = change_doc(report, report["changes"][seq], seq)
        temp.add_doc("change", key, text_fields, {**payload, "_preview_id": entry["id"]})
    matched = {h["_preview_id"] for h in temp.search(**search_kwargs)}
    return [e for e in added if e["id"] in matched]
