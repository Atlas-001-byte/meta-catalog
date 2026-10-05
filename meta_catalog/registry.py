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
        raw_edges = extract_ref_edges(name, version, document)
        edges: list[RefEdge] = []
        for e in raw_edges:
            dst_record = self._schemas.get((e.dst_schema, e.dst_version))
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

    def _logical_path_exists(
        self, schema: str, version: str, path: str, stack: frozenset[tuple[str, str]]
    ) -> bool:
        """判断逻辑字段路径是否可达，沿跨 Schema ``$ref`` 边跳转解析。"""
        if (schema, version) not in self._schemas:
            return False
        document = self._schemas[(schema, version)].document
        valid_paths = _expand_fields(document)
        if path in valid_paths:
            return True

        target_segs = ptr.parse(path)
        # 选择最长的、能覆盖目标路径前缀的出边。
        best = None
        for edge in self._edges.get((schema, version), ()):  # type: RefEdge
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
            best.dst_schema, best.dst_version, resolved, stack | {dst_key}
        )

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

        normalized: list[FieldRef] = []
        seen: set[tuple[str, str, str]] = set()
        for raw in refs or []:
            if isinstance(raw, FieldRef):
                r = raw
            else:
                r = FieldRef(raw["schema"], str(raw["version"]), raw.get("path", ""))
            validate_name(r.schema, what="引用 Schema 名称")
            record = self.get_schema(r.schema, r.version)
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
                        r.schema, r.version, cand, frozenset({(r.schema, r.version)})
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
        asset = Asset(asset_id, name, kind, tuple(normalized))
        self._assets[asset_id] = asset
        return asset

    def get_asset(self, asset_id: str) -> Asset:
        if asset_id not in self._assets:
            raise NotFoundError(f"资产 {asset_id} 不存在", details={"asset": asset_id})
        return self._assets[asset_id]

    def all_assets(self) -> list[Asset]:
        return [self._assets[k] for k in sorted(self._assets)]
