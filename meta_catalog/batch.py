"""原子批量登记（``MetaCatalog.register_batch``）的请求校验。

一批 Schema 与资产作为一个整体登记：要么全部成功、立即可读可检索，要么
整体失败、注册簿与检索索引不留任何部分登记效果。

批次内 ``Schema@版本`` 与资产 ``asset_id`` 不得重复；Schema 之间、资产与
Schema 之间互相可见——跨 Schema ``$ref`` 与资产 refs 可以指向本批资源，
前向引用与引用环沿用单注册的既有口径。

本模块只负责请求级结构校验与文档合法性校验；冲突与引用解析由
:class:`meta_catalog.registry.Registry` 的批量入口原子完成。错误口径：

  * ``resources`` 不是列表、条目结构 / 字段类型 / 必填项不合法、名称版本
    不满足命名规则、引用路径不是合法 JSON Pointer、批次内资源重复——
    :class:`BatchRegistrationInvalid`；
  * 资源总数超过 1000——:class:`BatchRegistrationTooLarge`；
  * Schema 文档不是合法 JSON Schema——
    :class:`SchemaComparisonInvalid`；
  * 与既有 ``Schema@版本`` 或 ``asset_id`` 冲突——
    :class:`AlreadyExistsError`；
  * 跨 Schema ``$ref`` 或资产 refs 指向的目标版本已存在（含本批）而字段
    不存在——:class:`NotFoundError`。
"""

from __future__ import annotations

from typing import Any

from . import limits
from . import pointer as ptr
from .errors import (
    BatchRegistrationInvalid,
    BatchRegistrationTooLarge,
    SchemaComparisonInvalid,
)
from .registry import validate_name as _validate_name
from .validator import validate_schema

SCHEMA = "schema"
ASSET = "asset"


def parse_resources(resources: Any) -> list[tuple]:
    """结构校验整批条目，返回与输入同序的内部条目元组。

    返回 ``("schema", name, version, document)`` 或
    ``("asset", asset_id, name, kind, refs)``；``refs`` 为规整后的
    ``[{"schema", "version", "path"}]``。任何结构问题抛
    :class:`BatchRegistrationInvalid`；总数超限时抛
    :class:`BatchRegistrationTooLarge`。
    """
    if not isinstance(resources, list):
        raise BatchRegistrationInvalid(
            "resources 必须是列表", details={"reason": "resources_not_list"}
        )
    if len(resources) > limits.MAX_BATCH_RESOURCES:
        raise BatchRegistrationTooLarge(
            f"批量登记资源总数不得超过 {limits.MAX_BATCH_RESOURCES}",
            details={
                "reason": "batch_too_large",
                "size": len(resources),
                "limit": limits.MAX_BATCH_RESOURCES,
            },
        )

    items: list[tuple] = []
    schema_keys: set[tuple[str, str]] = set()
    asset_ids: set[str] = set()

    for i, entry in enumerate(resources):
        if not isinstance(entry, dict):
            _invalid("批量登记条目必须是对象", "entry_not_object", i)
        rtype = entry.get("type")
        if rtype == SCHEMA:
            items.append(_parse_schema_entry(entry, i, schema_keys))
        elif rtype == ASSET:
            items.append(_parse_asset_entry(entry, i, asset_ids))
        else:
            _invalid(
                "条目 type 必须是 schema 或 asset",
                "invalid_type", i, value=rtype,
            )

    return items


def validate_documents(items: list[tuple]) -> None:
    """按输入顺序校验 Schema 文档合法性（SchemaComparisonInvalid）。"""
    for item in items:
        if item[0] == SCHEMA:
            _, name, version, document = item
            try:
                validate_schema(document)
            except SchemaComparisonInvalid as exc:
                exc.details.setdefault("schema", name)
                exc.details.setdefault("version", version)
                raise


# --------------------------------------------------------------- 条目解析
def _invalid(message: str, reason: str, index: int, **extra: Any) -> None:
    raise BatchRegistrationInvalid(
        message, details={"reason": reason, "index": index, **extra}
    )


def _parse_schema_entry(
    entry: dict, index: int, schema_keys: set[tuple[str, str]]
) -> tuple:
    name = entry.get("name")
    version = entry.get("version")
    if not isinstance(name, str) or not name:
        _invalid("schema 条目的 name 必须是非空字符串", "invalid_name", index)
    if not isinstance(version, str) or not version:
        _invalid(
            "schema 条目的 version 必须是非空字符串", "invalid_version", index
        )
    try:
        _validate_name(name, what="Schema 名称")
        _validate_name(version, what="版本号")
    except ValueError as exc:
        _invalid(str(exc), "invalid_name", index)
    if "document" not in entry:
        _invalid(
            "schema 条目缺少必填字段 document", "missing_document", index
        )
    key = (name, version)
    if key in schema_keys:
        _invalid(
            f"批次内 Schema {name}@{version} 重复",
            "duplicate_schema", index, schema=name, version=version,
        )
    schema_keys.add(key)
    return (SCHEMA, name, version, entry["document"])


def _parse_asset_entry(
    entry: dict, index: int, asset_ids: set[str]
) -> tuple:
    asset_id = entry.get("asset_id")
    name = entry.get("name")
    kind = entry.get("kind")
    if not isinstance(asset_id, str) or not asset_id:
        _invalid(
            "asset 条目的 asset_id 必须是非空字符串", "invalid_asset_id", index
        )
    try:
        _validate_name(asset_id, what="资产标识")
    except ValueError as exc:
        _invalid(str(exc), "invalid_asset_id", index)
    if not isinstance(name, str) or not name:
        _invalid(
            "asset 条目的 name 必须是非空字符串", "invalid_asset_name", index
        )
    if not isinstance(kind, str) or not kind:
        _invalid(
            "asset 条目的 kind 必须是非空字符串", "invalid_asset_kind", index
        )
    if asset_id in asset_ids:
        _invalid(
            f"批次内资产 {asset_id} 重复",
            "duplicate_asset", index, asset=asset_id,
        )
    refs = entry.get("refs", [])
    if refs is None:
        refs = []
    if not isinstance(refs, list):
        _invalid("asset 条目的 refs 必须是列表", "invalid_refs", index)
    refs = _parse_refs(refs, index)
    asset_ids.add(asset_id)
    return (ASSET, asset_id, name, kind, refs)


def _parse_refs(refs: list[Any], index: int) -> list[dict[str, str]]:
    """校验单个资产条目的 refs 结构与指针语法（不做存在性解析）。"""
    out: list[dict[str, str]] = []
    for j, raw in enumerate(refs):
        if not isinstance(raw, dict):
            _invalid(
                "资产 refs 每项必须是对象", "ref_not_object", index, ref_index=j
            )
        schema = raw.get("schema")
        version = raw.get("version")
        if not isinstance(schema, str) or not schema:
            _invalid(
                "资产引用的 schema 必须是非空字符串",
                "invalid_ref_schema", index, ref_index=j,
            )
        if not isinstance(version, str) or not version:
            _invalid(
                "资产引用的 version 必须是非空字符串",
                "invalid_ref_version", index, ref_index=j,
            )
        try:
            _validate_name(schema, what="引用 Schema 名称")
        except ValueError as exc:
            _invalid(str(exc), "invalid_ref_schema", index, ref_index=j)
        path = raw.get("path", "")
        if path is None:
            path = ""
        if not isinstance(path, str):
            _invalid(
                "资产引用的 path 必须是字符串",
                "invalid_ref_path", index, ref_index=j,
            )
        if path and not path.startswith("/"):
            _invalid(
                f"资产引用字段路径必须是 JSON Pointer: {path!r}",
                "invalid_pointer", index, ref_index=j, path=path,
            )
        try:
            ptr.parse(path)
        except ValueError:
            _invalid(
                f"资产引用字段路径不是合法 JSON Pointer: {path!r}",
                "invalid_pointer", index, ref_index=j, path=path,
            )
        if not _valid_pointer_escapes(path):
            _invalid(
                f"资产引用字段路径不是合法 JSON Pointer（~ 后只允许 0 或 1）: {path!r}",
                "invalid_pointer", index, ref_index=j, path=path,
            )
        out.append({"schema": schema, "version": version, "path": path})
    return out


def _valid_pointer_escapes(path: str) -> bool:
    """逐段检查 JSON Pointer 转义：``~`` 后只能是 ``0`` 或 ``1``。"""
    for segment in (path.split("/")[1:] if path else ()):
        k = 0
        while k < len(segment):
            if segment[k] == "~":
                if k + 1 >= len(segment) or segment[k + 1] not in "01":
                    return False
                k += 2
            else:
                k += 1
    return True
