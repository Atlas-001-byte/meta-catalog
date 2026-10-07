"""注册簿：Schema 版本、目录资产与跨 Schema 引用关系。

注册内容不可变：任何读取都返回深拷贝，比较、影响分析和检索都无法改动
既有注册结果、版本关系与资产依赖关系。

跨 Schema 引用约定：``$ref`` 写作 ``"<SchemaName>@<version>#<JSONPointer>"``，
例如 ``"Address@1.0#/properties/street"``；``#`` 后为空表示引用文档根。
文档内引用沿用标准 ``"#/..."`` 形式。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from . import limits
from . import pointer as ptr
from .errors import (
    AlreadyExistsError,
    BatchRegistrationInvalid,
    BatchRegistrationTooLarge,
    NotFoundError,
)
from .schema_fields import expand as _expand_fields

_REF_RE = re.compile(r"^(?P<name>[^@#/\s]+)@(?P<version>[^@#\s]+)#(?P<pointer>.*)$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def validate_name(name: str, *, what: str = "名称") -> None:
    if not isinstance(name, str) or not name or not _NAME_RE.match(name):
        raise ValueError(f"非法{what}: {name!r}（只允许字母、数字、_、.、-）")


@dataclass(frozen=True)
class SchemaVersion:
    name: str
    version: str
    document: Any
    title: str | None


@dataclass(frozen=True)
class FieldRef:
    """资产对某 Schema 某版本某字段路径的直接引用。"""

    schema: str
    version: str
    path: str  # JSON Pointer，"" 表示根


@dataclass(frozen=True)
class Asset:
    id: str
    name: str
    kind: str
    refs: tuple[FieldRef, ...]


@dataclass(frozen=True)
class RefEdge:
    """一条跨 Schema ``$ref`` 边：src 字段定义引用了 dst 字段节点。"""

    src_schema: str
    src_version: str
    src_path: str
    dst_schema: str
    dst_version: str
    dst_path: str


def parse_external_ref(ref: str) -> tuple[str, str, str] | None:
    """解析跨 Schema 引用；非此外部形式返回 ``None``。"""
    m = _REF_RE.match(ref)
    if not m:
        return None
    return m.group("name"), m.group("version"), m.group("pointer")


def extract_ref_edges(name: str, version: str, document: Any) -> list[RefEdge]:
    """提取文档内全部跨 Schema 引用边（原始文档指针，按出现顺序排序）。

    指针在注册时再结合两端文档解析为逻辑字段路径。
    """
    edges: list[RefEdge] = []

    def walk(node: Any, path: str, seen: frozenset[int]) -> None:
        if isinstance(node, bool) or not isinstance(node, (dict, list)):
            return
        if id(node) in seen:
            return
        seen = seen | {id(node)}
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                parsed = parse_external_ref(ref)
                if parsed is not None:
                    dst_name, dst_version, fragment = parsed
                    if fragment and not fragment.startswith("/"):
                        fragment = "/" + fragment
                    edges.append(
                        RefEdge(name, version, path, dst_name, dst_version, fragment)
                    )
            for k in sorted(node.keys()):
                walk(node[k], path + "/" + _escape(k) if path else "/" + _escape(k), seen)
        else:
            for i, v in enumerate(node):
                walk(v, f"{path}/{i}" if path else f"/{i}", seen)

    walk(document, "", frozenset())
    edges.sort(key=lambda e: (e.src_path, e.dst_schema, e.dst_version, e.dst_path))
    return edges


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _record_title(document: Any) -> str | None:
    if isinstance(document, dict):
        t = document.get("title")
        return t if isinstance(t, str) else None
    return None


def _resolve_schema_edges(
    name: str,
    version: str,
    document: Any,
    lookup,
) -> list[RefEdge]:
    """解析文档内跨 Schema 引用边为逻辑字段路径。

    ``lookup(name, version)`` 返回目标 :class:`SchemaVersion` 或 ``None``；
    目标缺失时允许前向引用（按纯文本规整），目标存在但字段不存在时抛
    :class:`NotFoundError`。
    """
    raw_edges = extract_ref_edges(name, version, document)
    edges: list[RefEdge] = []
    for e in raw_edges:
        dst_record = lookup(e.dst_schema, e.dst_version)
        dst_raw = e.dst_path if e.dst_path else ""
        if dst_record is not None:
            dst_logical = ptr.resolve_logical(dst_record.document, dst_raw)
            if dst_logical is None or dst_logical not in _expand_fields(dst_record.document):
                # 目标已注册但字段不存在：属于确定的非法引用。
                raise NotFoundError(
                    f"跨 Schema 引用 {e.dst_schema}@{e.dst_version}{dst_raw} 的目标字段不存在",
                    details={
                        "schema": e.dst_schema,
                        "version": e.dst_version,
                        "path": dst_raw,
                    },
                )
            final_dst = dst_logical
        else:
            final_dst = ptr.normalize_field_pointer(dst_raw)
        src_logical = ptr.resolve_logical(document, e.src_path)
        edges.append(
            RefEdge(
                name,
                version,
                src_logical or ptr.normalize_field_pointer(e.src_path),
                e.dst_schema,
                e.dst_version,
                final_dst,
            )
        )
    edges.sort(key=lambda x: (x.src_path, x.dst_schema, x.dst_version, x.dst_path))
    return edges


class Registry:
    def __init__(self) -> None:
        self._schemas: dict[tuple[str, str], SchemaVersion] = {}
        # name -> 按注册顺序保存的版本列表
        self._versions: dict[str, list[str]] = {}
        self._assets: dict[str, Asset] = {}
        self._edges: dict[tuple[str, str], tuple[RefEdge, ...]] = {}

    # ------------------------------------------------------------------ schema
    def register_schema(self, name: str, version: str, document: Any) -> SchemaVersion:
        validate_name(name, what="Schema 名称")
        validate_name(version, what="版本号")
        key = (name, version)
        if key in self._schemas:
            raise AlreadyExistsError(
                f"Schema {name}@{version} 已注册，注册内容不可覆盖",
                details={"schema": name, "version": version},
            )
        # 跨 Schema 引用允许前向（目标稍后注册），因此引用环也可注册；
        # 目标缺失或目标字段不存在时，影响传播自然不会产生命中。
        edges = _resolve_schema_edges(
            name, version, document, lambda n, v: self._schemas.get((n, v))
        )
        record = SchemaVersion(name, version, copy.deepcopy(document), _record_title(document))
        self._schemas[key] = record
        self._versions.setdefault(name, []).append(version)
        self._edges[key] = tuple(edges)
        return record

    def get_schema(self, name: str, version: str) -> SchemaVersion:
        key = (name, version)
        if key not in self._schemas:
            raise NotFoundError(
                f"Schema {name}@{version} 不存在",
                details={"schema": name, "version": version},
            )
        return self._schemas[key]

    def has_schema(self, name: str, version: str) -> bool:
        return (name, version) in self._schemas

    def schema_document(self, name: str, version: str) -> Any:
        return copy.deepcopy(self.get_schema(name, version).document)

    def list_versions(self, name: str) -> list[str]:
        if name not in self._versions:
            raise NotFoundError(f"Schema {name} 不存在", details={"schema": name})
        return list(self._versions[name])

    def edges(self, name: str, version: str) -> tuple[RefEdge, ...]:
        return self._edges.get((name, version), ())

    def all_edges(self) -> list[RefEdge]:
        out: list[RefEdge] = []
        for key in sorted(self._edges):
            out.extend(self._edges[key])
        return out

    def all_schemas(self) -> list[SchemaVersion]:
        return [self._schemas[k] for k in sorted(self._schemas)]

    def registered_pairs(self) -> list[tuple[str, str]]:
        """按全局注册顺序返回全部 ``(名称, 版本)``。"""
        return list(self._schemas.keys())

    def logical_path_exists(self, schema: str, version: str, path: str) -> bool:
        """判断逻辑字段路径是否可达（含沿跨 Schema ``$ref`` 跳转解析）。"""
        return self._logical_path_exists(
            schema, version, path, frozenset({(schema, version)}), None, None
        )

    def _view_get(
        self,
        view_schemas: dict[tuple[str, str], SchemaVersion] | None,
        name: str,
        version: str,
    ) -> SchemaVersion | None:
        """按「本批视图优先、既有注册簿其次」解析 Schema 版本。"""
        if view_schemas is not None:
            record = view_schemas.get((name, version))
            if record is not None:
                return record
        return self._schemas.get((name, version))

    def _logical_path_exists(
        self,
        schema: str,
        version: str,
        path: str,
        stack: frozenset[tuple[str, str]],
        view_schemas: dict[tuple[str, str], SchemaVersion] | None,
        view_edges: dict[tuple[str, str], tuple[RefEdge, ...]] | None,
    ) -> bool:
        """判断逻辑字段路径是否可达，沿跨 Schema ``$ref`` 边跳转解析。

        ``view_schemas`` / ``view_edges`` 给出尚未提交的本批 Schema 视图；
        路径解析与出边选择均按「本批优先、既有其次」合并。
        """
        record = self._view_get(view_schemas, schema, version)
        if record is None:
            return False
        valid_paths = _expand_fields(record.document)
        if path in valid_paths:
            return True

        target_segs = ptr.parse(path)
        # 选择最长的、能覆盖目标路径前缀的出边。
        out_edges: tuple[RefEdge, ...] = ()
        if view_edges is not None and (schema, version) in view_edges:
            out_edges = view_edges[(schema, version)]
        else:
            out_edges = self._edges.get((schema, version), ())
        best = None
        for edge in out_edges:  # type: RefEdge
            esegs = ptr.parse(edge.src_path)
            if len(esegs) <= len(target_segs) and target_segs[: len(esegs)] == esegs:
                if best is None or len(esegs) > len(ptr.parse(best.src_path)):
                    best = edge
        if best is None:
            return False
        dst_key = (best.dst_schema, best.dst_version)
        if dst_key in stack:
            return False
        suffix = target_segs[len(ptr.parse(best.src_path)) :]
        resolved = ptr.format(list(ptr.parse(best.dst_path)) + list(suffix))
        return self._logical_path_exists(
            best.dst_schema,
            best.dst_version,
            resolved,
            stack | {dst_key},
            view_schemas,
            view_edges,
        )

    def _normalize_asset_refs(
        self,
        refs: list[FieldRef] | list[dict] | None,
        view_schemas: dict[tuple[str, str], SchemaVersion] | None = None,
        view_edges: dict[tuple[str, str], tuple[RefEdge, ...]] | None = None,
    ) -> list[FieldRef]:
        """把资产 refs 解析、去重、排序为逻辑字段路径引用。

        引用解析口径与 :meth:`register_asset` 完全一致，但 Schema 与引用边
        可来自尚未提交的本批视图（本批优先、既有其次）。目标 Schema 缺失抛
        :class:`NotFoundError`，字段不可达同样抛 :class:`NotFoundError`。
        """
        normalized: list[FieldRef] = []
        seen: set[tuple[str, str, str]] = set()
        for raw in refs or []:
            if isinstance(raw, FieldRef):
                r = raw
            else:
                r = FieldRef(raw["schema"], str(raw["version"]), raw.get("path", ""))
            validate_name(r.schema, what="引用 Schema 名称")
            record = self._view_get(view_schemas, r.schema, r.version)
            if record is None:
                raise NotFoundError(
                    f"Schema {r.schema}@{r.version} 不存在",
                    details={"schema": r.schema, "version": r.version},
                )
            if r.path and not r.path.startswith("/"):
                raise ValueError(f"资产引用字段路径必须是 JSON Pointer: {r.path!r}")
            candidate_paths = []
            contextual = ptr.resolve_logical(record.document, r.path)
            if contextual is not None:
                candidate_paths.append(contextual)
            text_normalized = ptr.normalize_field_pointer(r.path)
            for cand in (text_normalized, r.path):
                if cand not in candidate_paths:
                    candidate_paths.append(cand)

            resolved_path = next(
                (
                    cand
                    for cand in candidate_paths
                    if self._logical_path_exists(
                        r.schema,
                        r.version,
                        cand,
                        frozenset({(r.schema, r.version)}),
                        view_schemas,
                        view_edges,
                    )
                ),
                None,
            )
            if resolved_path is None:
                raise NotFoundError(
                    f"资产引用的字段 {r.schema}@{r.version}{text_normalized} 不存在",
                    details={
                        "schema": r.schema,
                        "version": r.version,
                        "path": text_normalized,
                    },
                )
            r = FieldRef(r.schema, r.version, resolved_path)
            dedup_key = (r.schema, r.version, r.path)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            normalized.append(r)

        normalized.sort(key=lambda r: (r.schema, r.version, r.path))
        return normalized

    # ------------------------------------------------------------------- asset
    def register_asset(
        self,
        asset_id: str,
        name: str,
        kind: str,
        refs: list[FieldRef] | list[dict] | None = None,
    ) -> Asset:
        validate_name(asset_id, what="资产标识")
        if not isinstance(name, str) or not name:
            raise ValueError("资产名称必须是非空字符串")
        if not isinstance(kind, str) or not kind:
            raise ValueError("资产类型必须是非空字符串")
        if asset_id in self._assets:
            raise AlreadyExistsError(
                f"资产 {asset_id} 已注册", details={"asset": asset_id}
            )

        normalized = self._normalize_asset_refs(refs)
        asset = Asset(asset_id, name, kind, tuple(normalized))
        self._assets[asset_id] = asset
        return asset

    def get_asset(self, asset_id: str) -> Asset:
        if asset_id not in self._assets:
            raise NotFoundError(f"资产 {asset_id} 不存在", details={"asset": asset_id})
        return self._assets[asset_id]

    def all_assets(self) -> list[Asset]:
        return [self._assets[k] for k in sorted(self._assets)]

    # ------------------------------------------------------------- 原子批量登记
    def register_batch(
        self,
        resources: Any,
        document_validator=None,
    ) -> list[tuple[str, Any]]:
        """原子登记一批 Schema 与资产，成功时整体提交、失败时不留任何效果。

        ``resources`` 为列表，Schema 条目为
        ``{type:"schema", name, version, document}``，资产条目为
        ``{type:"asset", asset_id, name, kind, refs}``；``refs`` 口径与
        :meth:`register_asset` 一致，可指向本批 Schema（前向引用、引用环
        均允许）或既有 Schema。

        校验顺序固定为：条目结构 / 必填项 / 名称版本 / 引用指针与批次重复
        （:class:`BatchRegistrationInvalid`）→ Schema 文档合法性
        （``document_validator`` 抛 :class:`SchemaComparisonInvalid`）
        → 与既有资源冲突（:class:`AlreadyExistsError`）→ 跨 Schema 引用与
        资产引用字段存在性（:class:`NotFoundError`）。全部通过后才按输入
        顺序提交。

        返回按输入顺序的 ``[("schema", SchemaVersion), ("asset", Asset), ...]``。
        """
        parsed = self._parse_batch_resources(resources)

        # 文档合法性（SchemaComparisonInvalid）先于冲突与引用语义校验，
        # 与 register_schema 中 validate_schema 先于注册的既有顺序一致。
        if document_validator is not None:
            for entry in parsed:
                if entry[0] == "schema":
                    document_validator(entry[1]["document"])

        prepared, pending_edges = self._prepare_batch(parsed)
        return self._commit_batch(prepared, pending_edges)

    def _parse_batch_resources(self, resources: Any) -> list[tuple[str, dict]]:
        """结构校验并规整批次条目；失败抛 :class:`BatchRegistrationInvalid`。"""
        if not isinstance(resources, list):
            raise BatchRegistrationInvalid(
                "resources 必须是列表",
                details={"reason": "resources_not_list"},
            )
        if len(resources) > limits.MAX_BATCH_RESOURCES:
            raise BatchRegistrationTooLarge(
                f"批量登记资源总数超过限制 {limits.MAX_BATCH_RESOURCES}",
                details={
                    "reason": "too_many_resources",
                    "limit": limits.MAX_BATCH_RESOURCES,
                    "size": len(resources),
                },
            )

        seen_schemas: set[tuple[str, str]] = set()
        seen_assets: set[str] = set()
        parsed: list[tuple[str, dict]] = []

        for index, entry in enumerate(resources):
            if not isinstance(entry, dict):
                raise BatchRegistrationInvalid(
                    f"第 {index} 项不是字典",
                    details={"reason": "entry_not_mapping", "index": index},
                )
            entry_type = entry.get("type")
            if entry_type == "schema":
                parsed.append(("schema", self._parse_batch_schema(entry, index)))
                key = (parsed[-1][1]["name"], parsed[-1][1]["version"])
                if key in seen_schemas:
                    raise BatchRegistrationInvalid(
                        f"批次内 Schema {key[0]}@{key[1]} 重复",
                        details={
                            "reason": "duplicate_schema",
                            "index": index,
                            "schema": key[0],
                            "version": key[1],
                        },
                    )
                seen_schemas.add(key)
            elif entry_type == "asset":
                parsed.append(("asset", self._parse_batch_asset(entry, index)))
                asset_id = parsed[-1][1]["asset_id"]
                if asset_id in seen_assets:
                    raise BatchRegistrationInvalid(
                        f"批次内资产 {asset_id} 重复",
                        details={
                            "reason": "duplicate_asset",
                            "index": index,
                            "asset_id": asset_id,
                        },
                    )
                seen_assets.add(asset_id)
            else:
                raise BatchRegistrationInvalid(
                    f"第 {index} 项的 type 必须是 schema 或 asset: {entry_type!r}",
                    details={"reason": "invalid_type", "index": index, "type": entry_type},
                )
        return parsed

    @staticmethod
    def _batch_invalid(index: int, reason: str, message: str, **extra: Any):
        details = {"reason": reason, "index": index}
        details.update(extra)
        return BatchRegistrationInvalid(message, details=details)

    def _parse_batch_schema(self, entry: dict, index: int) -> dict:
        name = entry.get("name")
        if not isinstance(name, str):
            raise self._batch_invalid(
                index, "invalid_name", f"第 {index} 项的 name 必须是字符串", name=name
            )
        version = entry.get("version")
        if not isinstance(version, str):
            raise self._batch_invalid(
                index,
                "invalid_version",
                f"第 {index} 项的 version 必须是字符串",
                version=version,
            )
        try:
            validate_name(name, what="Schema 名称")
            validate_name(version, what="版本号")
        except ValueError as exc:
            raise self._batch_invalid(
                index,
                "invalid_name",
                f"第 {index} 项的名称或版本非法: {exc}",
                schema=name,
                version=version,
            ) from exc
        if "document" not in entry:
            raise self._batch_invalid(
                index, "missing_document", f"第 {index} 项缺少必填字段 document"
            )
        return {
            "name": name,
            "version": version,
            "document": entry["document"],
        }

    def _parse_batch_asset(self, entry: dict, index: int) -> dict:
        asset_id = entry.get("asset_id")
        if not isinstance(asset_id, str):
            raise self._batch_invalid(
                index,
                "invalid_asset_id",
                f"第 {index} 项的 asset_id 必须是字符串",
                asset_id=asset_id,
            )
        try:
            validate_name(asset_id, what="资产标识")
        except ValueError as exc:
            raise self._batch_invalid(
                index,
                "invalid_asset_id",
                f"第 {index} 项的资产标识非法: {exc}",
                asset_id=asset_id,
            ) from exc
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise self._batch_invalid(
                index, "invalid_name", f"第 {index} 项的 name 必须是非空字符串"
            )
        kind = entry.get("kind")
        if not isinstance(kind, str) or not kind:
            raise self._batch_invalid(
                index, "invalid_kind", f"第 {index} 项的 kind 必须是非空字符串"
            )
        raw_refs = entry.get("refs", [])
        if raw_refs is None:
            raw_refs = []
        if not isinstance(raw_refs, list):
            raise self._batch_invalid(
                index, "invalid_refs", f"第 {index} 项的 refs 必须是列表"
            )
        refs: list[dict[str, Any]] = []
        for j, raw in enumerate(raw_refs):
            if not isinstance(raw, dict):
                raise self._batch_invalid(
                    index,
                    "invalid_ref",
                    f"第 {index} 项 refs[{j}] 必须是字典",
                    ref_index=j,
                )
            schema = raw.get("schema")
            if not isinstance(schema, str):
                raise self._batch_invalid(
                    index,
                    "invalid_ref",
                    f"第 {index} 项 refs[{j}] 的 schema 必须是字符串",
                    ref_index=j,
                )
            version = raw.get("version")
            if not isinstance(version, str):
                raise self._batch_invalid(
                    index,
                    "invalid_ref",
                    f"第 {index} 项 refs[{j}] 的 version 必须是字符串",
                    ref_index=j,
                )
            try:
                validate_name(schema, what="引用 Schema 名称")
                validate_name(version, what="引用版本号")
            except ValueError as exc:
                raise self._batch_invalid(
                    index,
                    "invalid_ref",
                    f"第 {index} 项 refs[{j}] 的名称或版本非法: {exc}",
                    ref_index=j,
                ) from exc
            path = raw.get("path", "")
            if not isinstance(path, str):
                raise self._batch_invalid(
                    index,
                    "invalid_ref",
                    f"第 {index} 项 refs[{j}] 的 path 必须是字符串",
                    ref_index=j,
                )
            try:
                ptr.parse(path)
            except ValueError as exc:
                raise self._batch_invalid(
                    index,
                    "invalid_pointer",
                    f"第 {index} 项 refs[{j}] 的路径不是合法 JSON Pointer: {path!r}",
                    ref_index=j,
                    path=path,
                ) from exc
            if path and not path.startswith("/"):
                raise self._batch_invalid(
                    index,
                    "invalid_pointer",
                    f"第 {index} 项 refs[{j}] 的字段路径必须是 JSON Pointer: {path!r}",
                    ref_index=j,
                    path=path,
                )
            refs.append({"schema": schema, "version": version, "path": path})
        return {"asset_id": asset_id, "name": name, "kind": kind, "refs": refs}

    def _prepare_batch(
        self, parsed: list[tuple[str, dict]]
    ) -> tuple[
        list[tuple[str, Any]],
        dict[tuple[str, str], tuple[RefEdge, ...]],
    ]:
        """冲突校验与引用解析，生成待提交对象；任何失败都不改动注册簿。

        按输入顺序逐条构建本批 Schema 视图与资产：跨 Schema ``$ref`` 允许
        指向本批后续版本（前向引用）与引用环；目标版本在目录或本批中存在
        而字段不存在时抛 :class:`NotFoundError`。
        """
        # 与既有资源冲突（AlreadyExistsError）。
        for kind, entry in parsed:
            if kind == "schema":
                if (entry["name"], entry["version"]) in self._schemas:
                    raise AlreadyExistsError(
                        f"Schema {entry['name']}@{entry['version']} 已注册，注册内容不可覆盖",
                        details={"schema": entry["name"], "version": entry["version"]},
                    )
            elif entry["asset_id"] in self._assets:
                raise AlreadyExistsError(
                    f"资产 {entry['asset_id']} 已注册",
                    details={"asset": entry["asset_id"]},
                )

        # 先构建全部本批 Schema 记录与引用边（跨 Schema 引用可前向 / 成环）。
        view_schemas: dict[tuple[str, str], SchemaVersion] = {}
        view_edges: dict[tuple[str, str], tuple[RefEdge, ...]] = {}
        prepared_schemas: list[tuple[int, str, str, SchemaVersion]] = []
        for index, (kind, entry) in enumerate(parsed):
            if kind != "schema":
                continue
            name, version, document = entry["name"], entry["version"], entry["document"]
            record = SchemaVersion(
                name, version, copy.deepcopy(document), _record_title(document)
            )
            view_schemas[(name, version)] = record
            prepared_schemas.append((index, name, version, record))
        for index, name, version, record in prepared_schemas:
            def lookup(n: str, v: str, _vs=view_schemas) -> SchemaVersion | None:
                rec = _vs.get((n, v))
                return rec if rec is not None else self._schemas.get((n, v))

            view_edges[(name, version)] = tuple(
                _resolve_schema_edges(name, version, record.document, lookup)
            )

        # 资产 refs 按「本批 + 既有」合并注册表解析。
        prepared: list[tuple[str, Any]] = []
        schema_by_index = {item[0]: item[3] for item in prepared_schemas}
        for index, (kind, entry) in enumerate(parsed):
            if kind == "schema":
                prepared.append(("schema", schema_by_index[index]))
            else:
                normalized = self._normalize_asset_refs(
                    entry["refs"], view_schemas, view_edges
                )
                prepared.append(
                    (
                        "asset",
                        Asset(
                            entry["asset_id"],
                            entry["name"],
                            entry["kind"],
                            tuple(normalized),
                        ),
                    )
                )
        return prepared, view_edges

    def _commit_batch(
        self,
        prepared: list[tuple[str, Any]],
        pending_edges: dict[tuple[str, str], tuple[RefEdge, ...]],
    ) -> list[tuple[str, Any]]:
        """按输入顺序提交全部待提交对象（在全部校验通过后调用）。"""
        for kind, obj in prepared:
            if kind == "schema":
                key = (obj.name, obj.version)
                self._schemas[key] = obj
                self._versions.setdefault(obj.name, []).append(obj.version)
                self._edges[key] = pending_edges[key]
            else:
                self._assets[obj.id] = obj
        return prepared
