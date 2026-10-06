"""全文检索。

索引三类文档：已注册 Schema、目录资产、字段级变更报告中的每条变更。
变更报告生成后自动进入检索范围；新增文档类型不会改变旧查询的结果语义与
排序——排序完全由匹配分与确定性键决定，与索引插入顺序无关。

分词口径（对普通关键词保持唯一、稳定的匹配口径）：
  * 拉丁字母/数字连续串作为一个词条（小写）；
  * 中日韩字符连续串按二元组切分（单字保留单字）；
  * 同一查询内多个关键词词条之间为 AND。

:meth:`SearchIndex.search_page` 在与 :meth:`SearchIndex.search` 完全相同的
查询口径上提供游标分页：每页返回 ``items`` / ``total`` / ``page_size`` /
``next_cursor``。游标不透明，绑定完整查询条件与 ``page_size``，并带按索引
实例签发的签名，跨实例或与当前查询不匹配一律无效。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from .errors import SearchQueryInvalid

_TOKEN_RE = re.compile(r"[0-9a-z_]+|[一-鿿]+", re.IGNORECASE)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# search_page 接受的全部结构化过滤条件（keyword 与 page_size、cursor 除外）。
_PAGE_FILTER_KEYS = (
    "doc_type",
    "schema",
    "version",
    "field_path",
    "change_kind",
    "compatibility",
    "asset_name",
)
_CURSOR_VERSION = 1


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
        # 游标签名密钥：游标只对本索引实例有效（“来源未知”即无法验签）。
        self._cursor_secret = secrets.token_bytes(32)

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
        hits = self._run_query(
            keyword,
            {
                "doc_type": doc_type,
                "schema": schema,
                "version": version,
                "field_path": field_path,
                "change_kind": change_kind,
                "compatibility": compatibility,
                "asset_name": asset_name,
            },
        )
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
        page_size: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """与 :meth:`search` 同条件的游标分页检索（只读，不写索引）。

        第一页不传 ``cursor``；后续页只传上一页返回的 ``next_cursor``。
        返回 ``{"items", "total", "page_size", "next_cursor"}``：``items``
        与同条件 :meth:`search` 的结果逐项同构、顺序一致；``total`` 为调用
        时索引命中总数；末页 ``next_cursor`` 为 ``None``。任何参数不合法或
        游标无效 / 与当前查询不匹配时抛 :class:`SearchQueryInvalid`。
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
        page_size = _validate_page_inputs(keyword, filters, page_size, cursor)
        query_fingerprint = _fingerprint(keyword, filters)

        offset = 0
        if cursor is not None:
            offset = self._decode_cursor(cursor, query_fingerprint, page_size)

        hits = self._run_query(keyword, filters)
        total = len(hits)
        page = hits[offset : offset + page_size]
        next_cursor: str | None = None
        if offset + len(page) < total:
            next_cursor = self._encode_cursor(
                query_fingerprint, page_size, offset + page_size
            )
        return {
            "items": page,
            "total": total,
            "page_size": page_size,
            "next_cursor": next_cursor,
        }

    # ------------------------------------------------------------ 查询执行
    def _run_query(
        self, keyword: str | None, filters: dict[str, str | None]
    ) -> list[dict[str, Any]]:
        """与 :meth:`search` 相同口径执行一次检索，返回全部命中（稳定排序）。"""
        terms = tokenize(keyword) if keyword else []
        asset_terms = tokenize(filters["asset_name"]) if filters["asset_name"] else []

        results: list[tuple[int, tuple, dict[str, Any]]] = []
        for doc in self._docs:
            if filters["doc_type"] is not None and doc.doc_type != filters["doc_type"]:
                continue
            p = doc.payload

            if not self._passes_filters(
                doc,
                p,
                filters["schema"],
                filters["version"],
                filters["field_path"],
                filters["change_kind"],
                filters["compatibility"],
                asset_terms,
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
        return [r[2] for r in results]

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

    # ------------------------------------------------------------------ cursors
    def _encode_cursor(
        self, query_fingerprint: str, page_size: int, offset: int
    ) -> str:
        """生成不透明游标：载荷（版本/查询指纹/页大小/偏移）+ 按本索引签发的签名。"""
        body = json.dumps(
            [_CURSOR_VERSION, query_fingerprint, page_size, offset],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        payload = base64.urlsafe_b64encode(body).rstrip(b"=")
        sig = hmac.new(self._cursor_secret, payload, hashlib.sha256).digest()
        signature = base64.urlsafe_b64encode(sig).rstrip(b"=")
        return (payload + b"." + signature).decode("ascii")

    def _decode_cursor(
        self, cursor: Any, query_fingerprint: str, page_size: int
    ) -> int:
        """校验并解析游标，返回下一页起始偏移。

        内容缺失、格式非法、来源未知（验签失败）或与当前查询条件 /
        ``page_size`` 不匹配时统一抛 :class:`SearchQueryInvalid`。
        """
        invalid = SearchQueryInvalid(
            "cursor 无效或与当前查询不匹配",
            details={"reason": "invalid_cursor"},
        )
        parts = cursor.split(".")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise invalid
        payload_b, signature_b = parts
        try:
            expected_sig = hmac.new(
                self._cursor_secret, payload_b.encode("ascii"), hashlib.sha256
            ).digest()
            given_sig = base64.urlsafe_b64decode(
                signature_b + "=" * (-len(signature_b) % 4)
            )
            body = base64.urlsafe_b64decode(payload_b + "=" * (-len(payload_b) % 4))
        except (ValueError, TypeError, UnicodeEncodeError) as exc:
            raise invalid from exc
        if not hmac.compare_digest(given_sig, expected_sig):
            raise invalid
        try:
            decoded = json.loads(body.decode("utf-8"))
            version, bound_fp, bound_size, offset = decoded
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise invalid from exc
        if (
            version != _CURSOR_VERSION
            or bound_fp != query_fingerprint
            or bound_size != page_size
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset <= 0
        ):
            raise invalid
        return offset


# ============================================================ search_page 校验
def _validate_page_inputs(
    keyword: Any,
    filters: dict[str, Any],
    page_size: Any,
    cursor: Any,
) -> int:
    """校验 search_page 入参，返回规范化的 page_size；失败抛 SearchQueryInvalid。"""
    if not isinstance(keyword, str) and keyword is not None:
        raise SearchQueryInvalid(
            "keyword 必须是字符串或 None",
            details={"reason": "invalid_keyword"},
        )
    for name, value in filters.items():
        if not isinstance(value, str) and value is not None:
            raise SearchQueryInvalid(
                f"过滤条件 {name} 必须是字符串或 None",
                details={"reason": "invalid_filter", "filter": name},
            )
    if not isinstance(page_size, int) or isinstance(page_size, bool):
        raise SearchQueryInvalid(
            "page_size 必须是 1 到 200 的普通整数",
            details={"reason": "invalid_page_size"},
        )
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise SearchQueryInvalid(
            "page_size 必须在 1 到 200 之间",
            details={"reason": "invalid_page_size", "page_size": page_size},
        )
    if cursor is not None and not isinstance(cursor, str):
        raise SearchQueryInvalid(
            "cursor 必须是字符串或 None",
            details={"reason": "invalid_cursor"},
        )
    return page_size


def _fingerprint(keyword: str | None, filters: dict[str, str | None]) -> str:
    """完整查询条件（含 keyword 与全部结构化过滤）的稳定指纹。

    条件任一项变化都会得到不同指纹，从而无法复用旧游标。
    """
    material = json.dumps(
        [keyword] + [filters[k] for k in _PAGE_FILTER_KEYS],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
