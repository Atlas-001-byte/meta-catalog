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

from .errors import AlreadyExistsError, NotFoundError
from . import pointer as ptr
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


def _build_schema_edges(
    name: str,
    version: str,
    document: Any,
    schemas_view: dict[tuple[str, str], SchemaVersion],
) -> list[RefEdge]:
    """逻辑化文档内全部跨 Schema 引用边。

    ``schemas_view`` 为解析目标时可见的版本表（单注册时即当前注册簿，
    批量登记时为含本批资源的影子注册簿）。目标版本可见时结合目标文档解析
    片段，目标字段不存在抛 :class:`NotFoundError`；目标版本不可见时按文本
    规整为逻辑路径——跨 Schema 引用允许前向（目标稍后注册），引用环也可
    注册。
    """
    raw_edges = extract_ref_edges(name, version, document)
    edges: list[RefEdge] = []
    for e in raw_edges:
        dst_record = schemas_view.get((e.dst_schema, e.dst_version))
        dst_raw = e.dst_path if e.dst_path else ""
        if dst_record is not None:
            dst_logical = ptr.resolve_logical(dst_record.document, dst_raw)
            if dst_logical is None or dst_logical not in _expand_fields(dst_record.document):
                # 目标已注册（或在本批内）但字段不存在：属于确定的非法引用。
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
        edges = _build_schema_edges(name, version, document, self._schemas)
        title = None
        if isinstance(document, dict):
            t = document.get("title")
            title = t if isinstance(t, str) else None
        record = SchemaVersion(name, version, copy.deepcopy(document), title)
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
            self._schemas,
            self._edges,
            schema,
            version,
            path,
            frozenset({(schema, version)}),
        )

    @staticmethod
    def _logical_path_exists(
        schemas_view: dict[tuple[str, str], SchemaVersion],
        edges_view: dict[tuple[str, str], tuple[RefEdge, ...]],
        schema: str,
        version: str,
        path: str,
        stack: frozenset[tuple[str, str]],
    ) -> bool:
        """判断逻辑字段路径是否可达，沿跨 Schema ``$ref`` 边跳转解析。

        ``schemas_view`` / ``edges_view`` 为解析时可见的版本表与引用边表
        （批量登记时包含本批尚未提交的资源）。
        """
        if (schema, version) not in schemas_view:
            return False
        document = schemas_view[(schema, version)].document
        valid_paths = _expand_fields(document)
        if path in valid_paths:
            return True

        target_segs = ptr.parse(path)
        # 选择最长的、能覆盖目标路径前缀的出边。
        best = None
        for edge in edges_view.get((schema, version), ()):  # type: RefEdge
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
        return Registry._logical_path_exists(
            schemas_view,
            edges_view,
            best.dst_schema,
            best.dst_version,
            resolved,
            stack | {dst_key},
        )

    # ------------------------------------------------------------- 批量登记
    def register_batch(
        self, items: list[tuple]
    ) -> list[tuple[str, Any]]:
        """原子登记一批已通过结构校验的资源。

        ``items`` 每项为 ``("schema", name, version, document)`` 或
        ``("asset", asset_id, name, kind, refs)``（``refs`` 为原始 dict
        列表）。批次内 Schema 之间、资产与 Schema 之间互相可见：跨 Schema
        ``$ref`` 与资产 refs 可指向本批资源，前向引用与引用环按单注册同口径
        处理。

        任何一项与既有资源冲突（:class:`AlreadyExistsError`）或引用字段
        不存在（:class:`NotFoundError`）时整体失败，本注册簿不留任何部分
        登记效果。成功返回按输入顺序的
        ``[("schema", SchemaVersion), ..., ("asset", Asset)]``。
        """
        # ---- 阶段 1：收集本批 Schema 记录并完成全部冲突检查（不触碰既有注册簿）。
        new_records: dict[tuple[str, str], SchemaVersion] = {}
        batch_versions: dict[str, list[str]] = {}
        new_asset_ids: set[str] = set()
        for item in items:
            if item[0] == "schema":
                _, name, version, document = item
                key = (name, version)
                if key in self._schemas:
                    raise AlreadyExistsError(
                        f"Schema {name}@{version} 已注册，注册内容不可覆盖",
                        details={"schema": name, "version": version},
                    )
                if key in new_records:
                    # 结构校验阶段已拦截批次内重复，这里作防御性兜底。
                    raise AlreadyExistsError(
                        f"批次内 Schema {name}@{version} 重复",
                        details={"schema": name, "version": version},
                    )
                title = None
                if isinstance(document, dict):
                    t = document.get("title")
                    title = t if isinstance(t, str) else None
                new_records[key] = SchemaVersion(
                    name, version, copy.deepcopy(document), title
                )
                batch_versions.setdefault(name, []).append(version)
            else:
                asset_id = item[1]
                if asset_id in self._assets:
                    raise AlreadyExistsError(
                        f"资产 {asset_id} 已注册", details={"asset": asset_id}
                    )
                if asset_id in new_asset_ids:
                    raise AlreadyExistsError(
                        f"批次内资产 {asset_id} 重复", details={"asset": asset_id}
                    )
                new_asset_ids.add(asset_id)

        # ---- 阶段 2：在「既有 + 本批」版本表上逻辑化全部跨 Schema 引用边。
        schemas_view: dict[tuple[str, str], SchemaVersion] = {
            **self._schemas,
            **new_records,
        }
        new_edges: dict[tuple[str, str], tuple[RefEdge, ...]] = {}
        for key in sorted(new_records):
            record = new_records[key]
            new_edges[key] = tuple(
                _build_schema_edges(
                    record.name, record.version, record.document, schemas_view
                )
            )
        edges_view: dict[tuple[str, str], tuple[RefEdge, ...]] = {
            **self._edges,
            **new_edges,
        }

        # ---- 阶段 3：解析资产 refs（目标版本与引用边均含本批资源）。
        new_assets: dict[str, Asset] = {}
        for item in items:
            if item[0] != "asset":
                continue
            _, asset_id, name, kind, refs = item
            normalized = self._normalize_asset_refs(
                refs, schemas_view, edges_view
            )
            new_assets[asset_id] = Asset(asset_id, name, kind, tuple(normalized))

        # ---- 阶段 4：全部校验通过后一次性提交。
        self._schemas.update(new_records)
        for name, versions in batch_versions.items():
            self._versions.setdefault(name, []).extend(versions)
        self._edges.update(new_edges)
        self._assets.update(new_assets)

        created: list[tuple[str, Any]] = []
        for item in items:
            if item[0] == "schema":
                created.append(("schema", new_records[(item[1], item[2])]))
            else:
                created.append(("asset", new_assets[item[1]]))
        return created

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

        normalized = self._normalize_asset_refs(
            refs or [], self._schemas, self._edges
        )
        asset = Asset(asset_id, name, kind, tuple(normalized))
        self._assets[asset_id] = asset
        return asset

    @staticmethod
    def _normalize_asset_refs(
        refs: list[FieldRef] | list[dict],
        schemas_view: dict[tuple[str, str], SchemaVersion],
        edges_view: dict[tuple[str, str], tuple[RefEdge, ...]],
    ) -> list[FieldRef]:
        """把资产 refs 结合可见版本表规整为去重排序后的逻辑字段引用。

        目标版本不存在或引用字段不可达时抛 :class:`NotFoundError`；路径不
        是 JSON Pointer 时抛 :class:`ValueError`（与单注册口径一致，由
        上层转换为批量登记的结构错误）。
        """
        normalized: list[FieldRef] = []
        seen: set[tuple[str, str, str]] = set()
        for raw in refs or []:
            if isinstance(raw, FieldRef):
                r = raw
            else:
                r = FieldRef(raw["schema"], str(raw["version"]), raw.get("path", ""))
            validate_name(r.schema, what="引用 Schema 名称")
            record = schemas_view.get((r.schema, r.version))
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
                    if Registry._logical_path_exists(
                        schemas_view,
                        edges_view,
                        r.schema,
                        r.version,
                        cand,
                        frozenset({(r.schema, r.version)}),
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

    def get_asset(self, asset_id: str) -> Asset:
        if asset_id not in self._assets:
            raise NotFoundError(f"资产 {asset_id} 不存在", details={"asset": asset_id})
        return self._assets[asset_id]

    def all_assets(self) -> list[Asset]:
        return [self._assets[k] for k in sorted(self._assets)]
