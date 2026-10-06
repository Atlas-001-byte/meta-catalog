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

import base64
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .errors import SearchQueryInvalid

_TOKEN_RE = re.compile(r"[0-9a-z_]+|[一-鿿]+", re.IGNORECASE)


def _facet_entries(counter: Counter) -> list[dict[str, Any]]:
    """正计数分面条目，按 count 降序、value 升序。"""
    return [
        {"value": value, "count": counter[value]}
        for value in sorted(counter, key=lambda v: (-counter[v], v))
    ]

# 分页游标：不透明串，绑定完整查询条件与 page_size，并携带续页位置。
_CURSOR_MAGIC = "mcsp1"
_CURSOR_SALT = "meta_catalog.search_page.v1"
_PAGE_SIZE_DEFAULT = 50
_PAGE_SIZE_MIN = 1
_PAGE_SIZE_MAX = 200
_PAGE_FILTERS = (
    "doc_type",
    "schema",
    "version",
    "field_path",
    "change_kind",
    "compatibility",
    "asset_name",
)


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


def _query_fingerprint(
    keyword: str | None, filters: dict[str, Any], page_size: int
) -> str:
    """查询条件与 page_size 的确定性指纹，用于校验游标归属。"""
    canonical = json.dumps(
        {"keyword": keyword, "page_size": page_size, **filters},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cursor_digest(payload_b64: str) -> str:
    return hashlib.sha256(
        (_CURSOR_SALT + "." + payload_b64).encode("ascii")
    ).hexdigest()[:16]


def _encode_cursor(fingerprint: str, position: tuple) -> str:
    """把查询指纹与续页位置编码为不透明游标。"""
    neg_hits, key = position
    payload = json.dumps(
        {"v": 1, "q": fingerprint, "hits": -neg_hits, "key": list(key)},
        ensure_ascii=False,
        sort_keys=True,
    )
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return f"{_CURSOR_MAGIC}.{payload_b64}.{_cursor_digest(payload_b64)}"


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
        results = self._sorted_hits(
            keyword,
            doc_type=doc_type,
            schema=schema,
            version=version,
            field_path=field_path,
            change_kind=change_kind,
            compatibility=compatibility,
            asset_name=asset_name,
        )
        hits = [item for _, _, item in results]
        return hits[:limit] if limit is not None else hits

    def search_page(
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
        page_size: int = _PAGE_SIZE_DEFAULT,
        cursor: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """分页检索。条件与排序口径和 :meth:`search` 完全一致；只读索引。

        第一页不传 ``cursor``；后续页只传上一页返回的 ``next_cursor``。
        游标绑定全部查询条件与 ``page_size``，任何一项变化都不得复用。
        返回 ``items``、``total``（调用时索引命中总数）、``page_size``、
        ``next_cursor``（末页为 None）。参数不合法时抛
        :class:`SearchQueryInvalid`。
        """
        filters = {
            "doc_type": doc_type,
            "schema": schema,
            "version": version,
            "field_path": field_path,
            "change_kind": change_kind,
            "compatibility": compatibility,
            "asset_name": asset_name,
        }
        self._validate_page_args(keyword, filters, page_size, extra)

        position = None
        if cursor is not None:
            position = self._decode_cursor(cursor)
            expected = _query_fingerprint(keyword, filters, page_size)
            if position[0] != expected:
                raise SearchQueryInvalid(
                    "cursor 与当前查询条件或 page_size 不匹配",
                    details={"reason": "cursor_query_mismatch"},
                )
            position = position[1]

        results = self._sorted_hits(keyword, **filters)
        total = len(results)
        if position is not None:
            results = [r for r in results if (-r[0], r[1]) > position]

        page = results[:page_size]
        next_cursor = None
        if len(results) > page_size:
            last_hits, last_key, _ = page[-1]
            next_cursor = _encode_cursor(
                _query_fingerprint(keyword, filters, page_size),
                (-last_hits, last_key),
            )
        return {
            "items": [item for _, _, item in page],
            "total": total,
            "page_size": page_size,
            "next_cursor": next_cursor,
        }

    def search_facets(
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
        **extra: Any,
    ) -> dict[str, Any]:
        """在与 :meth:`search` 完全相同的完整命中集合上做只读聚合。

        不拉取命中条目即可查看文档类型、Schema 名称、``名称@版本``、
        变更性质、兼容结论与受影响资产的分布；聚合不受任何 limit 截断。
        返回 ``total`` 与六个分面（``doc_type``、``schema``、``version``、
        ``change_kind``、``compatibility``、``asset``），仅列正计数条目，
        各分面按 ``count`` 降序、``value``（asset 按 ``name``）升序。

        ``keyword`` 与过滤值只接受字符串或 None；``limit``、``page_size``、
        ``cursor``、``offset`` 及其他未公开参数一律抛
        :class:`SearchQueryInvalid`。
        """
        filters = {
            "doc_type": doc_type,
            "schema": schema,
            "version": version,
            "field_path": field_path,
            "change_kind": change_kind,
            "compatibility": compatibility,
            "asset_name": asset_name,
        }
        self._validate_facet_args(keyword, filters, extra)

        hits = self._sorted_hits(keyword, **filters)
        counts = {
            "doc_type": Counter(),
            "schema": Counter(),
            "version": Counter(),
            "change_kind": Counter(),
            "compatibility": Counter(),
            "asset": Counter(),
        }
        asset_names: dict[str, str] = {}
        for _, _, item in hits:
            self._collect_facets(item, counts, asset_names)

        return {
            "total": len(hits),
            "doc_type": _facet_entries(counts["doc_type"]),
            "schema": _facet_entries(counts["schema"]),
            "version": _facet_entries(counts["version"]),
            "change_kind": _facet_entries(counts["change_kind"]),
            "compatibility": _facet_entries(counts["compatibility"]),
            "asset": [
                {
                    "asset_id": aid,
                    "name": asset_names[aid],
                    "count": counts["asset"][aid],
                }
                for aid in sorted(
                    counts["asset"],
                    key=lambda a: (-counts["asset"][a], asset_names[a], a),
                )
            ],
        }

    # ------------------------------------------------------------- 分面校验
    @staticmethod
    def _validate_facet_args(
        keyword: Any, filters: dict[str, Any], extra: dict[str, Any]
    ) -> None:
        if extra:
            raise SearchQueryInvalid(
                f"未公开的检索参数: {sorted(extra)}",
                details={"reason": "unknown_argument", "arguments": sorted(extra)},
            )
        for name, value in (("keyword", keyword), *filters.items()):
            if value is not None and not isinstance(value, str):
                raise SearchQueryInvalid(
                    f"{name} 必须是字符串或 None",
                    details={"reason": "filter_not_string", "argument": name},
                )

    @staticmethod
    def _collect_facets(
        item: dict[str, Any],
        counts: dict[str, Counter],
        asset_names: dict[str, str],
    ) -> None:
        """按一篇命中文档累加各分面；同篇同值只计一次。"""
        doc_type = item["type"]
        counts["doc_type"][doc_type] += 1

        if doc_type == "schema":
            # Schema 文档名称取自身；版本取自身版本（名称@版本）。
            counts["schema"][item["name"]] += 1
            counts["version"][f"{item['name']}@{item['version']}"] += 1
        elif doc_type == "asset":
            # 资产取全部引用 Schema / 版本，同篇同值去重；资产自身计入一次。
            for name in set(item["schemas"]):
                counts["schema"][name] += 1
            for nv in set(item["versions"]):
                counts["version"][nv] += 1
            aid = item["id"]
            counts["asset"][aid] += 1
            asset_names.setdefault(aid, item["name"])
        else:  # change：只对字段变更统计 change_kind / compatibility
            counts["schema"][item["schema"]] += 1
            versions = {item["baseline_version"]}
            if item.get("candidate_version"):
                versions.add(item["candidate_version"])
            for v in versions:
                counts["version"][f"{item['schema']}@{v}"] += 1
            counts["change_kind"][item["change_kind"]] += 1
            counts["compatibility"][item["compatibility"]] += 1
            # 直接与传递影响资产的并集，每资产一次。
            for a in item["matched_assets"]:
                aid = a["asset_id"]
                counts["asset"][aid] += 1
                asset_names.setdefault(aid, a["name"])

    # ------------------------------------------------------------- 分页校验
    @staticmethod
    def _validate_page_args(
        keyword: Any,
        filters: dict[str, Any],
        page_size: Any,
        extra: dict[str, Any],
    ) -> None:
        if extra:
            raise SearchQueryInvalid(
                f"未公开的检索参数: {sorted(extra)}",
                details={"reason": "unknown_argument", "arguments": sorted(extra)},
            )
        if isinstance(page_size, bool) or not isinstance(page_size, int):
            raise SearchQueryInvalid(
                "page_size 必须是 1 到 200 的普通整数",
                details={"reason": "page_size_invalid", "page_size": repr(page_size)},
            )
        if not (_PAGE_SIZE_MIN <= page_size <= _PAGE_SIZE_MAX):
            raise SearchQueryInvalid(
                "page_size 必须在 1 到 200 之间",
                details={"reason": "page_size_out_of_range", "page_size": page_size},
            )
        for name, value in (("keyword", keyword), *filters.items()):
            if value is not None and not isinstance(value, str):
                raise SearchQueryInvalid(
                    f"{name} 必须是字符串或 None",
                    details={"reason": "filter_not_string", "argument": name},
                )

    @staticmethod
    def _decode_cursor(cursor: Any) -> tuple[str, tuple]:
        if not isinstance(cursor, str) or not cursor:
            raise SearchQueryInvalid(
                "cursor 缺失内容或不是字符串",
                details={"reason": "cursor_invalid"},
            )
        parts = cursor.split(".")
        if len(parts) != 3 or parts[0] != _CURSOR_MAGIC:
            raise SearchQueryInvalid(
                "cursor 格式非法或来源未知",
                details={"reason": "cursor_invalid"},
            )
        payload_b64, digest = parts[1], parts[2]
        expect = _cursor_digest(payload_b64)
        if digest != expect:
            raise SearchQueryInvalid(
                "cursor 校验失败（格式非法或来源未知）",
                details={"reason": "cursor_invalid"},
            )
        try:
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
        except Exception:
            raise SearchQueryInvalid(
                "cursor 内容无法解析",
                details={"reason": "cursor_invalid"},
            ) from None
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or not isinstance(payload.get("q"), str)
            or not isinstance(payload.get("hits"), int)
            or isinstance(payload.get("hits"), bool)
            or not isinstance(payload.get("key"), list)
        ):
            raise SearchQueryInvalid(
                "cursor 内容结构非法",
                details={"reason": "cursor_invalid"},
            )
        position = (-payload["hits"], tuple(payload["key"]))
        return payload["q"], position

    def _sorted_hits(
        self,
        keyword: str | None,
        *,
        doc_type: str | None = None,
        schema: str | None = None,
        version: str | None = None,
        field_path: str | None = None,
        change_kind: str | None = None,
        compatibility: str | None = None,
        asset_name: str | None = None,
    ) -> list[tuple[int, tuple, dict[str, Any]]]:
        """返回按（命中字段数降序、确定性键升序）排序的
        ``(命中字段数, 确定性键, 条目)`` 列表。"""
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
        return results

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
