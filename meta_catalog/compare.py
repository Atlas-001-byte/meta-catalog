"""字段级破坏性变更识别。

输入两个同一 Schema 标识下的 JSON Schema 文档（基线版本、候选版本）与显式
字段重命名映射，产出确定性的字段级变更报告。

变更种类（``change_kind``）：
  * ``added``            新增字段（非必填兼容 / 必填破坏）
  * ``deleted``          删除字段（破坏）
  * ``modified``         结构定义变化（按收窄/放宽判定）
  * ``required_added``   既有字段新增必填约束（破坏）
  * ``required_removed`` 既有字段解除必填约束（兼容）
  * ``rename``           显式声明的重命名（整棵子树按一次 rename 计入）
  * ``metadata``         仅标题/描述/注释变化（元数据变更）

兼容结论（``compatibility``）：``breaking`` / ``compatible`` / ``metadata``。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import limits, pointer as ptr, schema_fields as sf
from .errors import ImpactAnalysisTooLarge
from .registry import Registry

RENAMED = "rename"
ADDED = "added"
DELETED = "deleted"
MODIFIED = "modified"
REQUIRED_ADDED = "required_added"
REQUIRED_REMOVED = "required_removed"
METADATA = "metadata"

BREAKING = "breaking"
COMPATIBLE = "compatible"
METADATA_COMPAT = "metadata"


def _required_set(fields: dict[str, sf.Field]) -> set[str]:
    """收集文档内全部被标记为必填的字段路径（已合并 allOf 的 required）。"""
    out: set[str] = set()
    for f in fields.values():
        out.update(f.required_paths)
    return out


def _summary(schema: dict) -> dict[str, Any]:
    # required 是父节点对子字段的约束，不放进字段自身摘要。
    return {k: v for k, v in sf.summary(schema).items() if k != "required"}


def validate_renames(
    renames: list | None,
    old_fields: dict[str, sf.Field],
    new_fields: dict[str, sf.Field],
) -> list[tuple[str, str]]:
    """校验并规范化重命名映射，失败时抛 :class:`SchemaComparisonInvalid`。"""
    from .errors import SchemaComparisonInvalid

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
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            src, dst = item[0], item[1]
        else:
            src = dst = None
        if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst:
            raise SchemaComparisonInvalid(
                f"第 {i} 条重命名映射必须包含非空字符串 from 与 to",
                details={"reason": "rename_invalid", "index": i},
            )
        pairs.append((src, dst))

    # 1) 起点/终点必须存在于对应版本。
    for src, dst in pairs:
        if src not in old_fields:
            raise SchemaComparisonInvalid(
                f"重命名起点 {src} 在基线版本中不存在",
                details={"reason": "rename_from_not_found", "path": src},
            )
        if dst not in new_fields:
            raise SchemaComparisonInvalid(
                f"重命名终点 {dst} 在候选版本中不存在",
                details={"reason": "rename_to_not_found", "path": dst},
            )

    # 2) 同一路径不得被重复映射（任一端点出现两次，或起点等于终点）。
    seen: set[str] = set()
    for src, dst in pairs:
        for ep in (src, dst):
            if ep in seen:
                raise SchemaComparisonInvalid(
                    f"路径 {ep} 在重命名映射中被重复使用",
                    details={"reason": "rename_duplicate", "path": ep},
                )
            seen.add(ep)

    # 端点两两之间子树不得重叠，否则同一路径被隐式映射两次。
    ordered = sorted(seen, key=lambda p: ptr.parse(p))
    for a, b in zip(ordered, ordered[1:]):
        if ptr.is_under(a, b):
            raise SchemaComparisonInvalid(
                f"重命名端点 {a} 与 {b} 的子树重叠，映射不明确",
                details={"reason": "rename_overlapping", "paths": [a, b]},
            )

    # 3) 跨版本一致性：起点不应仍在候选版本、终点不应已在基线版本。
    for src, dst in pairs:
        if src in new_fields:
            raise SchemaComparisonInvalid(
                f"重命名起点 {src} 仍存在于候选版本，映射跨版本不一致",
                details={"reason": "rename_cross_version_inconsistent", "path": src},
            )
        if dst in old_fields:
            raise SchemaComparisonInvalid(
                f"重命名终点 {dst} 已存在于基线版本，映射跨版本不一致",
                details={"reason": "rename_cross_version_inconsistent", "path": dst},
            )

    return pairs


def _entry(
    change_kind: str,
    compatibility: str,
    old_path: str | None,
    new_path: str | None,
    old_schema: dict | None,
    new_schema: dict | None,
    impacts: dict | None,
) -> dict[str, Any]:
    return {
        "path": new_path if new_path is not None else old_path,
        "old_path": old_path,
        "new_path": new_path,
        "old_summary": _summary(old_schema) if old_schema is not None else None,
        "new_summary": _summary(new_schema) if new_schema is not None else None,
        "change_kind": change_kind,
        "compatibility": compatibility,
        "direct_assets": list(impacts["direct_assets"]) if impacts else [],
        "transitive_assets": list(impacts["transitive_assets"]) if impacts else [],
    }


def _compare_matched(
    old_field: sf.Field, new_field: sf.Field, old_required: bool, new_required: bool
) -> tuple[str, str] | None:
    """比较两侧对齐的同字段，返回 (change_kind, compatibility) 或 None。"""
    old_s, new_s = old_field.schema, new_field.schema
    struct_changed = sf.structural_signature(old_s) != sf.structural_signature(new_s)

    if not struct_changed:
        if old_required == new_required:
            annotation_changed = (
                sf.annotation_signature(old_s) != sf.annotation_signature(new_s)
            )
            return (METADATA, METADATA_COMPAT) if annotation_changed else None
        if new_required:
            return REQUIRED_ADDED, BREAKING
        return REQUIRED_REMOVED, COMPATIBLE

    verdict = sf.classify_pair(old_s, new_s)
    if new_required and not old_required:
        verdict = BREAKING  # 结构变化与新增必填同时发生，取更严格结论
    return MODIFIED, verdict


def build_report(
    registry: Registry,
    name: str,
    baseline_version: str,
    candidate_document: Any,
    candidate_version: str | None,
    renames: list | None,
) -> dict[str, Any]:
    """构造字段级变更报告（不读取候选注册内容、不改动任何注册数据）。"""
    from .impact import analyze_field

    old_doc = registry.get_schema(name, baseline_version).document

    old_fields = sf.expand(old_doc)
    new_fields = sf.expand(candidate_document)
    if len(old_fields) > limits.MAX_FIELDS or len(new_fields) > limits.MAX_FIELDS:
        raise ImpactAnalysisTooLarge(
            "分析对象字段数量超过公开限制",
            details={
                "reason": "fields_exceeded",
                "limit": limits.MAX_FIELDS,
                "baseline_fields": len(old_fields),
                "candidate_fields": len(new_fields),
            },
        )
    old_required = _required_set(old_fields)
    new_required = _required_set(new_fields)

    pairs = validate_renames(renames, old_fields, new_fields)
    rename_srcs = dict(pairs)
    rename_dsts = {d: s for s, d in pairs}

    def in_rename_subtree(path: str, roots: dict[str, str]) -> str | None:
        """返回包含该路径的重命名根（路径按段序排序，最近的根唯一）。"""
        segs = ptr.parse(path)
        best = None
        for root in roots:
            rsegs = ptr.parse(root)
            if len(rsegs) <= len(segs) and segs[: len(rsegs)] == rsegs:
                if best is None or len(rsegs) > len(ptr.parse(best)):
                    best = root
        return best

    # 重命名子树对齐：old 后缀路径 -> new 后缀路径（仅当两侧都存在）。
    aligned_old: dict[str, str] = {}
    aligned_new: dict[str, str] = {}
    for src, dst in pairs:
        src_segs, dst_segs = ptr.parse(src), ptr.parse(dst)
        for p in old_fields:
            psegs = ptr.parse(p)
            if len(psegs) <= len(src_segs) or psegs[: len(src_segs)] != src_segs:
                continue
            q = ptr.format(dst_segs + psegs[len(src_segs) :])
            if q in new_fields:
                aligned_old[p] = q
                aligned_new[q] = p

    changes: list[dict[str, Any]] = []

    def impacts_for(old_path: str) -> dict:
        return analyze_field(registry, name, baseline_version, old_path)

    # 1) 重命名根：每条映射恰好一条 rename（不再计删除+新增）。
    for src, dst in sorted(pairs):
        changes.append(
            _entry(
                RENAMED,
                BREAKING,
                src,
                dst,
                old_fields[src].schema,
                new_fields[dst].schema,
                impacts_for(src),
            )
        )

    # 2) 旧侧：子树对齐字段做配对比较；其余旧侧独有路径为 deleted。
    for p in sorted(old_fields, key=lambda x: ptr.parse(x)):
        if p in rename_srcs:
            continue
        if in_rename_subtree(p, rename_srcs) is not None:
            q = aligned_old.get(p)
            if q is None:
                changes.append(
                    _entry(
                        DELETED, BREAKING, p, None,
                        old_fields[p].schema, None, impacts_for(p),
                    )
                )
            else:
                decision = _compare_matched(
                    old_fields[p], new_fields[q], p in old_required, q in new_required
                )
                if decision is not None:
                    changes.append(
                        _entry(
                            decision[0], decision[1], p, q,
                            old_fields[p].schema, new_fields[q].schema,
                            impacts_for(p),
                        )
                    )
            continue
        if p in new_fields:
            continue  # 公共路径，第 4 步处理
        changes.append(
            _entry(
                DELETED, BREAKING, p, None,
                old_fields[p].schema, None, impacts_for(p),
            )
        )

    # 3) 新侧：重命名子树中未覆盖的新路径与其余新侧独有路径为 added。
    for q in sorted(new_fields, key=lambda x: ptr.parse(x)):
        if q in rename_dsts:
            continue
        if in_rename_subtree(q, rename_dsts) is not None:
            if q in aligned_new:
                continue  # 已在第 2 步配对比较
        elif q in old_fields:
            continue  # 公共路径，第 4 步处理
        changes.append(
            _entry(
                ADDED,
                BREAKING if q in new_required else COMPATIBLE,
                None,
                q,
                None,
                new_fields[q].schema,
                None,
            )
        )

    # 4) 非重命名公共路径。
    for p in sorted(old_fields, key=lambda x: ptr.parse(x)):
        if p in rename_srcs or in_rename_subtree(p, rename_srcs) is not None:
            continue
        if p not in new_fields:
            continue
        decision = _compare_matched(
            old_fields[p], new_fields[p], p in old_required, p in new_required
        )
        if decision is not None:
            changes.append(
                _entry(
                    decision[0], decision[1], p, p,
                    old_fields[p].schema, new_fields[p].schema, impacts_for(p),
                )
            )

    if len(changes) > limits.MAX_CHANGES:
        raise ImpactAnalysisTooLarge(
            "变更条目数量超过公开限制",
            details={"reason": "changes_exceeded", "limit": limits.MAX_CHANGES},
        )

    # 确定性排序：按展示路径段、路径文本、变更种类。
    changes.sort(
        key=lambda e: (
            ptr.parse(e["path"]),
            e["path"],
            e["change_kind"],
            e["old_path"] or "",
        )
    )

    counts = {BREAKING: 0, COMPATIBLE: 0, METADATA_COMPAT: 0}
    for e in changes:
        counts[e["compatibility"]] += 1

    canonical = json.dumps(
        {
            "schema": name,
            "baseline_version": baseline_version,
            "candidate_version": candidate_version,
            "candidate_document": candidate_document,
            "renames": sorted(pairs),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    report_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    return {
        "report_id": report_id,
        "schema": name,
        "baseline_version": baseline_version,
        "candidate_version": candidate_version,
        "renames": [{"from": s, "to": d} for s, d in sorted(pairs)],
        "summary": {
            "total": len(changes),
            "breaking": counts[BREAKING],
            "compatible": counts[COMPATIBLE],
            "metadata": counts[METADATA_COMPAT],
        },
        "changes": changes,
    }
