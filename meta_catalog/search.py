"""全文检索。

索引三类文档：已注册 Schema、目录资产、字段级变更报告中的每条变更。
变更报告生成后自动进入检索范围；新增文档类型不会改变旧查询的结果语义与
排序——排序完全由匹配分与确定性键决定，与索引插入顺序无关。

分词口径（对普通关键词保持唯一、稳定的匹配口径）：
  * 拉丁字母/数字连续串作为一个词条（小写）；
  * 中日韩字符连续串按二元组切分（单字保留单字）；
  * 同一查询内多个关键词词条之间为 AND。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_TOKEN_RE = re.compile(r"[0-9a-z_]+|[一-鿿]+", re.IGNORECASE)


def tokenize(text: str | None) -> list[str]:
    if text is None:
        return []
    tokens: list[str] = []
    for piece in _TOKEN_RE.findall(str(text).lower()):
        if "一" <= piece[0] <= "鿿":
            if len(piece) == 1:
                tokens.append(piece)
            else:
                tokens.extend(piece[i : i + 2] for i in range(len(piece) - 1))
        else:
            tokens.append(piece)
    return tokens


@dataclass
class IndexedDoc:
    doc_type: str  # "schema" | "asset" | "change"
    key: tuple  # 确定性排序键
    fields: dict[str, str]  # 命名字段 -> 原文（用于返回“命中字段”）
    payload: dict[str, Any]
    tokens: dict[str, frozenset[str]] = field(default_factory=dict)


class SearchIndex:
    def __init__(self) -> None:
        self._docs: list[IndexedDoc] = []

    def add_doc(
        self,
        doc_type: str,
        key: tuple,
        fields: dict[str, str],
        payload: dict[str, Any],
    ) -> None:
        tokens = {
            name: frozenset(tokenize(value))
            for name, value in fields.items()
            if value is not None
        }
        self._docs.append(IndexedDoc(doc_type, tuple(key), dict(fields), payload, tokens))

    def search(
        self,
        keyword: str | None = None,
        *,
        doc_type: str | None = None,
        schema: str | None = None,
        version: str | None = None,
        field_path: str | None = None,
        change_kind: str | None = None,
        compatibility: str | None = None,
        asset_name: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """组合检索。结构化过滤条件之间为 AND；keyword 在可检索文本字段上 AND
        全命中。返回结果按（命中字段数降序、确定性键升序）稳定排序。"""
        terms = tokenize(keyword) if keyword else []
        asset_terms = tokenize(asset_name) if asset_name else []

        results: list[tuple[int, tuple, dict[str, Any]]] = []
        for doc in self._docs:
            if doc_type is not None and doc.doc_type != doc_type:
                continue
            p = doc.payload

            if not self._passes_filters(
                doc, p, schema, version, field_path, change_kind, compatibility, asset_terms
            ):
                continue

            hit_fields: set[str] = set()
            for fname, toks in doc.tokens.items():
                if terms and all(t in toks for t in terms):
                    hit_fields.add(fname)
            if terms and not hit_fields:
                continue

            if asset_terms:
                hit_fields |= self._asset_hit_fields(doc, asset_terms)

            item = {
                "type": doc.doc_type,
                "matched_fields": sorted(hit_fields),
                **p,
            }
            results.append((len(hit_fields), doc.key, item))

        results.sort(key=lambda r: (-r[0], r[1]))
        hits = [r[2] for r in results]
        return hits[:limit] if limit is not None else hits

    # ------------------------------------------------------------------ filters
    @staticmethod
    def _passes_filters(
        doc: IndexedDoc,
        p: dict,
        schema: str | None,
        version: str | None,
        field_path: str | None,
        change_kind: str | None,
        compatibility: str | None,
        asset_terms: list[str],
    ) -> bool:
        if doc.doc_type == "change":
            if schema is not None and p["schema"] != schema:
                return False
            if version is not None and version not in (
                p["baseline_version"],
                p["candidate_version"],
            ):
                return False
            if field_path is not None and field_path not in (
                p["path"],
                p.get("old_path"),
                p.get("new_path"),
            ):
                return False
            if change_kind is not None and p["change_kind"] != change_kind:
                return False
            if compatibility is not None and p["compatibility"] != compatibility:
                return False
            if asset_terms:
                names = doc.tokens.get("impact_assets", frozenset())
                ids = doc.tokens.get("impact_asset_ids", frozenset())
                if not all(t in names or t in ids for t in asset_terms):
                    return False
        elif doc.doc_type == "schema":
            if schema is not None and p["name"] != schema:
                return False
            if version is not None and p["version"] != version:
                return False
            if field_path is not None or change_kind is not None or compatibility is not None:
                return False
            if asset_terms:
                return False
        elif doc.doc_type == "asset":
            if schema is not None and schema not in p["schemas"]:
                return False
            if version is not None and version not in p["versions"]:
                return False
            if field_path is not None or change_kind is not None or compatibility is not None:
                return False
            if asset_terms:
                toks = doc.tokens.get("name", frozenset())
                if not all(t in toks for t in asset_terms):
                    return False
        return True

    @staticmethod
    def _asset_hit_fields(doc: IndexedDoc, asset_terms: list[str]) -> set[str]:
        hits: set[str] = set()
        for fname in ("impact_assets", "impact_asset_ids", "name"):
            toks = doc.tokens.get(fname)
            if toks and all(t in toks for t in asset_terms):
                hits.add(fname)
        return hits
