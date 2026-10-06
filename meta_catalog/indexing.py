"""检索索引的文档构造。

把字段级变更报告中的单条变更展开为检索文档（排序键、可检索文本字段、
负载）。:class:`meta_catalog.catalog.MetaCatalog` 入库时使用，
:mod:`meta_catalog.preview` 预检时用同一构造在临时索引上模拟检索效果，
保证两边口径一致。
"""

from __future__ import annotations

from typing import Any


def change_doc(
    report: dict[str, Any], change: dict[str, Any], seq: int
) -> tuple[tuple, dict[str, str], dict[str, Any]]:
    """构造一条变更检索文档：``(key, text_fields, payload)``。"""
    direct = change["direct_assets"]
    transitive = change["transitive_assets"]
    impacted: dict[str, str] = {}
    for a in direct:
        impacted[a["asset_id"]] = a["name"]
    for a in transitive:
        impacted.setdefault(a["asset_id"], a["name"])

    asset_text = "\n".join(sorted(impacted.values()))
    asset_id_text = "\n".join(sorted(impacted))
    summary_text = _summary_text(change["old_summary"]) + "\n" + _summary_text(
        change["new_summary"]
    )
    text_fields = {
        "schema": report["schema"],
        "version": " ".join(
            v for v in (report["baseline_version"], report["candidate_version"]) if v
        ),
        "field_path": "\n".join(
            p for p in (change["old_path"], change["new_path"], change["path"]) if p
        ),
        "change_kind": change["change_kind"],
        "compatibility": change["compatibility"],
        "impact_assets": asset_text,
        "impact_asset_ids": asset_id_text,
        "summary": summary_text,
    }
    key = (
        "change",
        report["schema"],
        report["baseline_version"],
        report["candidate_version"] or "",
        report["report_id"],
        change["path"],
        change["old_path"] or "",
        change["change_kind"],
        seq,
    )
    payload = {
        "report_id": report["report_id"],
        "schema": report["schema"],
        "baseline_version": report["baseline_version"],
        "candidate_version": report["candidate_version"],
        "path": change["path"],
        "old_path": change["old_path"],
        "new_path": change["new_path"],
        "change_kind": change["change_kind"],
        "compatibility": change["compatibility"],
        "old_summary": change["old_summary"],
        "new_summary": change["new_summary"],
        "matched_assets": [
            {"asset_id": aid, "name": impacted[aid]} for aid in sorted(impacted)
        ],
        "direct_assets": direct,
        "transitive_assets": transitive,
        "summary": {
            "report_id": report["report_id"],
            "schema": report["schema"],
            "path": change["path"],
            "change_kind": change["change_kind"],
            "compatibility": change["compatibility"],
            "assets": [
                {"asset_id": aid, "name": impacted[aid]} for aid in sorted(impacted)
            ],
        },
    }
    return key, text_fields, payload


def _summary_text(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    parts: list[str] = []
    for key, value in summary.items():
        parts.append(f"{key}={_flatten(value)}")
    return "\n".join(parts)


def _flatten(value: Any) -> str:
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)
