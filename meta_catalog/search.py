"""全文检索：资产关键词检索与变更报告组合检索。

分词与匹配口径（资产检索与报告检索共用，保证旧查询语义不变）：

- 大小写不敏感；按标识符边界（``/`` ``.`` ``@`` ``-`` ``_``、
  大小写边界、数字边界）拆词；中日韩文字按单字索引。
- 普通关键词查询按拆词后 **全部命中（AND）** 匹配。
- 排序稳定：命中词数降序，再按名称/标识字典序；无随机因素。
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable

from .comparison import ChangeKind, Compatibility
from .registry import Asset, Registry, version_sort_key

# 经典标识符拆词：连续大写 / 驼峰边界 / 数字
_SPLIT_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+|[一-鿿㐀-䶿]")


def tokenize(text: Any) -> list[str]:
    if text is None:
        return []
    if not isinstance(text, str):
        text = str(text)
    return [m.group(0).lower() for m in _SPLIT_RE.finditer(text)]


def tokens_match(bag: set[str], query_tokens: Iterable[str]) -> bool:
    return all(tok in bag for tok in query_tokens)


def _asset_bag(registry: Registry, asset: Asset) -> set[str]:
    tokens = set(tokenize(asset.asset_id))
    tokens.update(tokenize(asset.name))
    tokens.update(tokenize(asset.asset_type))
    tokens.update(tokenize(asset.description))
    for ref in asset.references:
        tokens.update(tokenize(ref.schema_name))
        tokens.update(tokenize(ref.field_path.replace("/", " ")))
    return tokens


class AssetSearchIndex:
    """资产关键词索引（基线能力；注册后内容不可变）。"""

    def __init__(self, registry: Registry):
        self._registry = registry

    def search(self, query: str, *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        wanted = tokenize(query)
        scored: list[tuple[int, str, str]] = []
        for asset in self._registry.list_assets():
            bag = _asset_bag(self._registry, asset)
            if wanted and not tokens_match(bag, wanted):
                continue
            # 命中词数越多越靠前；并列时按 asset_id 字典序，稳定
            score = len(set(wanted) & bag)
            scored.append((score, asset.asset_id, asset.name))
        scored.sort(key=lambda x: (-x[0], x[1]))
        if offset:
            scored = scored[offset:]
        if limit is not None:
            scored = scored[:limit]
        hits = []
        for _, asset_id, _ in scored:
            asset = self._registry.get_asset(asset_id)
            hits.append({
                "asset_id": asset.asset_id,
                "name": asset.name,
                "type": asset.asset_type,
                "description": asset.description,
                "references": [
                    {
                        "schema": r.schema_name,
                        "version": r.version,
                        "field_path": r.field_path or "/",
                    }
                    for r in asset.references
                ],
            })
        return hits


# ---------------------------------------------------------------------------
# 变更报告检索
# ---------------------------------------------------------------------------

def _change_text_bag(report: dict[str, Any], change: dict[str, Any]) -> set[str]:
    tokens = set(tokenize(report["schema"]))
    tokens.update(tokenize(report["baseline_version"]))
    tokens.update(tokenize(report["candidate_version"]))
    tokens.update(tokenize(change["field_path"].replace("/", " ")))
    tokens.update(tokenize(change["kind"]))
    tokens.update(tokenize(change["compatibility"]))
    if change.get("renamed_to"):
        tokens.update(tokenize(change["renamed_to"].replace("/", " ")))
    for asset in change.get("impacted_assets", []):
        tokens.update(tokenize(asset["asset_id"]))
        tokens.update(tokenize(asset["name"]))
        tokens.update(tokenize(asset["type"]))
    return tokens


def _asset_summary(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": asset["asset_id"],
        "name": asset["name"],
        "type": asset["type"],
        "depth": asset["depth"],
    }


class ChangeSearchIndex:
    """变更报告索引。报告只增不改，旧查询语义保持稳定。"""

    def __init__(self) -> None:
        # report_id -> report dict
        self._reports: dict[str, dict[str, Any]] = {}

    def add(self, report: dict[str, Any]) -> None:
        # 存副本：调用方对返回结果的修改不影响已入库报告
        self._reports[report["report_id"]] = copy.deepcopy(report)

    def get(self, report_id: str) -> dict[str, Any] | None:
        report = self._reports.get(report_id)
        return copy.deepcopy(report) if report is not None else None

    def list_reports(self) -> list[dict[str, Any]]:
        return [self._reports[k] for k in sorted(self._reports)]

    def search(
        self,
        *,
        schema: str | None = None,
        version: str | None = None,
        field_path: str | None = None,
        kind: str | None = None,
        compatibility: str | None = None,
        impacted_asset: str | None = None,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """组合检索（所有条件 AND）。返回命中字段与命中资产摘要。"""
        kind_val = _validate_enum(kind, ChangeKind, "kind")
        compat_val = _validate_enum(compatibility, Compatibility, "compatibility")
        wanted_tokens = tokenize(query) if query else []

        schema_tok = set(tokenize(schema)) if schema else None
        version_exact = version
        if field_path:
            normalized = field_path.lstrip("#")
            root_only = normalized in ("", "/")
            field_tokens = set() if root_only else set(tokenize(normalized.replace("/", " ")))
        else:
            root_only = False
            field_tokens = None
        asset_tokens = set(tokenize(impacted_asset)) if impacted_asset else None

        grouped: list[tuple[tuple, str, dict[str, Any]]] = []
        total_fields = 0
        for report in self._reports.values():
            matched_changes: list[dict[str, Any]] = []
            matched_assets: dict[str, dict[str, Any]] = {}
            for change in report["changes"]:
                if not _change_matches(
                    report, change,
                    schema_tok=schema_tok,
                    version_exact=version_exact,
                    field_tokens=field_tokens,
                    root_only=root_only,
                    kind_val=kind_val,
                    compat_val=compat_val,
                    asset_tokens=asset_tokens,
                    wanted_tokens=wanted_tokens,
                ):
                    continue
                matched_changes.append(change)
                for asset in change.get("impacted_assets", []):
                    # 资产条件只纳入真正命中该资产条件的资产摘要；
                    # 没有资产条件时纳入该字段下全部资产。
                    if asset_tokens is not None:
                        bag = set(tokenize(asset["asset_id"])) | set(tokenize(asset["name"]))
                        if not tokens_match(bag, asset_tokens):
                            continue
                    matched_assets.setdefault(asset["asset_id"], _asset_summary(asset))
            if not matched_changes:
                continue
            total_fields += len(matched_changes)
            matched_changes.sort(key=lambda c: (c["field_path"], c["kind"], c.get("renamed_to") or ""))
            asset_list = [matched_assets[k] for k in sorted(matched_assets)]
            hit = {
                "report_id": report["report_id"],
                "schema": report["schema"],
                "baseline_version": report["baseline_version"],
                "candidate_version": report["candidate_version"],
                "matched_fields": matched_changes,
                "matched_assets": asset_list,
            }
            sort_key = (
                report["schema"],
                version_sort_key(report["baseline_version"]),
                version_sort_key(report["candidate_version"]),
                report["report_id"],
            )
            grouped.append((sort_key, report["report_id"], hit))

        grouped.sort(key=lambda x: x[0])
        ordered = [item[2] for item in grouped]
        total = len(ordered)
        if offset:
            ordered = ordered[offset:]
        if limit is not None:
            ordered = ordered[:limit]
        return {"total": total, "total_fields": total_fields, "hits": ordered}


def _validate_enum(value: str | None, enum_cls: type, label: str) -> str | None:
    if value is None:
        return None
    try:
        return enum_cls(value).value
    except ValueError:
        allowed = ", ".join(sorted(v.value for v in enum_cls))  # type: ignore[attr-defined]
        raise ValueError(f"invalid {label}: {value!r}; allowed: {allowed}") from None


def _change_matches(
    report: dict[str, Any],
    change: dict[str, Any],
    *,
    schema_tok: set[str] | None,
    version_exact: str | None,
    field_tokens: set[str] | None,
    root_only: bool,
    kind_val: str | None,
    compat_val: str | None,
    asset_tokens: set[str] | None,
    wanted_tokens: list[str],
) -> bool:
    if schema_tok is not None:
        bag = set(tokenize(report["schema"]))
        if not tokens_match(bag, schema_tok):
            return False
    if version_exact is not None:
        if version_exact not in (report["baseline_version"], report["candidate_version"]):
            return False
    if field_tokens is not None:
        if root_only:
            if change["field_path"] != "/":
                return False
        else:
            bag = set(tokenize(change["field_path"].replace("/", " ")))
            if change.get("renamed_to"):
                bag |= set(tokenize(change["renamed_to"].replace("/", " ")))
            if not tokens_match(bag, field_tokens):
                return False
    if kind_val is not None and change["kind"] != kind_val:
        return False
    if compat_val is not None and change["compatibility"] != compat_val:
        return False
    if asset_tokens is not None:
        hit = False
        for asset in change.get("impacted_assets", []):
            bag = set(tokenize(asset["asset_id"])) | set(tokenize(asset["name"]))
            if tokens_match(bag, asset_tokens):
                hit = True
                break
        if not hit:
            return False
    if wanted_tokens:
        if not tokens_match(_change_text_bag(report, change), wanted_tokens):
            return False
    return True
