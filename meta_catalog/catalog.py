"""公开门面：:class:`MetaCatalog`。

组合注册簿、Schema 校验、字段级比较、影响分析与全文检索。所有成功结果都以
普通 dict/list 返回（可直接 JSON 序列化）；注册、比较、读取与检索均不会改动
既有 Schema 的注册内容、版本关系与资产依赖关系。
"""

from __future__ import annotations

import copy
from typing import Any

from . import audit as audit_mod
from . import compare as compare_mod
from . import indexing
from . import preview as preview_mod
from . import schema_fields as sf
from . import upgrade as upgrade_mod
from .errors import NotFoundError, SchemaComparisonInvalid
from .explain import explain_field
from .impact import analyze_field
from .registry import FieldRef, Registry
from .search import SearchIndex
from .validator import validate_schema


class MetaCatalog:
    def __init__(self) -> None:
        self._registry = Registry()
        self._index = SearchIndex()
        self._reports: dict[str, dict[str, Any]] = {}
        self._indexed_reports: set[str] = set()

    # ============================================================ 注册：Schema
    def register_schema(self, name: str, version: str, document: Any) -> dict[str, Any]:
        """注册一个不可变的 Schema 版本。文档不合法时抛 SchemaComparisonInvalid。"""
        validate_schema(document)
        record = self._registry.register_schema(name, version, document)
        self._index_schema(record.name, record.version, record.document, record.title)
        return self.get_schema(name, version)

    def get_schema(self, name: str, version: str) -> dict[str, Any]:
        record = self._registry.get_schema(name, version)
        return {
            "name": record.name,
            "version": record.version,
            "title": record.title,
            "document": copy.deepcopy(record.document),
        }

    def list_versions(self, name: str) -> list[str]:
        return self._registry.list_versions(name)

    # ============================================================== 注册：资产
    def register_asset(
        self,
        asset_id: str,
        name: str,
        kind: str,
        refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """注册目录资产。

        ``refs`` 为 ``[{"schema": 名, "version": 版本, "path": 字段指针}, ...]``，
        表示资产对字段的直接引用；路径可写文档指针（``/properties/a``）或
        逻辑字段路径（``/a``），注册时统一规整。
        """
        asset = self._registry.register_asset(asset_id, name, kind, refs or [])
        self._index_asset(asset.id, asset.name, asset.kind, list(asset.refs))
        return self._asset_view(asset)

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self._asset_view(self._registry.get_asset(asset_id))

    @staticmethod
    def _asset_view(asset) -> dict[str, Any]:
        return {
            "asset_id": asset.id,
            "name": asset.name,
            "kind": asset.kind,
            "refs": [
                {"schema": r.schema, "version": r.version, "path": r.path}
                for r in asset.refs
            ],
        }

    # ================================================================ 影响分析
    def analyze_impact(self, name: str, version: str, path: str) -> dict[str, Any]:
        """字段级影响分析：返回直接引用资产与经其他 Schema 传递引用的资产。"""
        return analyze_field(self._registry, name, version, path)

    # ============================================================ 影响链路解释
    def explain_impact(
        self,
        name: str,
        version: str,
        path: str,
        asset_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """按资产解释指定 Schema 版本中逻辑字段的影响链路（只读）。

        ``asset_ids`` 为 ``None`` 时覆盖全部资产；指定时去重且不考虑顺序，
        空列表不选任何资产，未知资产抛 :class:`NotFoundError`。所查 Schema、
        版本不存在或字段不可达时同样抛 :class:`NotFoundError`。

        返回 ``schema``、``version``、``path`` 与按 ``asset_id`` 升序的
        ``assets``；每个资产含 ``impact_kind``（``direct`` / ``transitive``
        / ``both``）与 ``chains``。直接命中是零步链（``source == target ==
        所查字段``）；传递命中给出沿跨 Schema ``$ref`` 的实际步进链，同一
        ``(source, target)`` 只保留一条最短链（等长取步进定位元组稳定最小
        者）。超过既有链深度、引用边或资产上限时抛
        :class:`ImpactAnalysisTooLarge`。

        本调用只读现有注册内容：不注册资源、不生成报告、不写入检索索引；
        返回值为独立副本，相同输入得到相同结果。
        """
        return explain_field(self._registry, name, version, path, asset_ids)

    # ============================================================ 引用完整性审计
    def check_schema_references(
        self, name: str | None = None, version: str | None = None
    ) -> dict[str, Any]:
        """审计已注册 Schema 版本中的跨 Schema ``$ref`` 完整性（只读）。

        无参数审计全部版本；只给 ``name`` 按注册顺序审计该名称的各版本；
        只给 ``version`` 审计同版本号的全部 Schema；同时给出时审计指定版本，
        无匹配版本抛 :class:`NotFoundError`。文档内 ``#/`` 引用不在审计范围。

        返回普通 dict（可直接 JSON 序列化），含 ``checked``、``total``、
        ``resolved``、``issues``、``references``；审计不改动注册内容、
        不写入检索索引，相同输入返回相同结果。
        """
        return copy.deepcopy(audit_mod.check_references(self._registry, name, version))

    # ======================================================== 字段级变更识别
    def compare_schemas(
        self,
        name: str,
        baseline_version: str,
        candidate: Any = None,
        *,
        candidate_version: str | None = None,
        renames: list[dict[str, str]] | list[list[str]] | None = None,
    ) -> dict[str, Any]:
        """比较基线版本与候选文档，生成并入库可检索的字段级变更报告。

        ``candidate`` 直接给出候选 JSON Schema 文档；或省略它、仅传
        ``candidate_version`` 以引用同 Schema 标识下已注册的候选版本。
        ``renames`` 为 ``[{"from": 旧路径, "to": 新路径}, ...]``。

        任何不合法情形统一抛 :class:`SchemaComparisonInvalid`。
        相同输入返回相同报告（幂等，不会重复入库）。
        """
        # 1) 解析候选文档与基线版本。
        candidate_document, label = self._resolve_comparison_inputs(
            name, baseline_version, candidate, candidate_version
        )

        # 2) 生成报告（重命名校验在其中完成）。
        report = compare_mod.build_report(
            self._registry,
            name,
            baseline_version,
            candidate_document,
            label,
            renames,
        )

        # 3) 幂等入库并进入全文检索范围。
        report_id = report["report_id"]
        if report_id not in self._reports:
            self._reports[report_id] = report
        if report_id not in self._indexed_reports:
            for i, change in enumerate(report["changes"]):
                self._index_change(report, change, i)
            self._indexed_reports.add(report_id)
        return copy.deepcopy(report)

    def _resolve_comparison_inputs(
        self,
        name: str,
        baseline_version: str,
        candidate: Any,
        candidate_version: str | None,
    ) -> tuple[Any, str | None]:
        """解析并校验比较输入：候选文档（深拷贝）与候选版本标签、基线版本存在性。

        校验口径与 :meth:`compare_schemas` 完全一致，失败时抛
        :class:`SchemaComparisonInvalid`。
        """
        if candidate is None:
            if candidate_version is None:
                raise SchemaComparisonInvalid(
                    "必须提供候选文档 candidate 或已注册的 candidate_version",
                    details={"reason": "candidate_missing"},
                )
            try:
                candidate_record = self._registry.get_schema(name, candidate_version)
            except NotFoundError as exc:
                raise SchemaComparisonInvalid(
                    f"指定版本 {name}@{candidate_version} 不存在",
                    details={"reason": "version_not_found", **exc.details},
                ) from exc
            candidate_document = copy.deepcopy(candidate_record.document)
            label = candidate_version
        else:
            validate_schema(candidate)
            candidate_document = copy.deepcopy(candidate)
            label = candidate_version  # 允许调用方给候选文档附带版本标签

        if not self._registry.has_schema(name, baseline_version):
            raise SchemaComparisonInvalid(
                f"指定版本 {name}@{baseline_version} 不存在",
                details={"reason": "version_not_found", "schema": name, "version": baseline_version},
            )
        return candidate_document, label

    # ============================================================ 升级影响汇总
    def analyze_upgrade_impact(
        self,
        name: str,
        baseline_version: str,
        candidate: Any = None,
        *,
        candidate_version: str | None = None,
        renames: list[dict[str, str]] | list[list[str]] | None = None,
        asset_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """汇总一次升级对目录资产的影响（按资产维度的字段级变更视图）。

        候选文档、候选版本、重命名映射与字段路径语义与
        :meth:`compare_schemas` 一致；``report_id`` 与相同输入的
        ``compare_schemas`` 相同。

        ``asset_ids`` 为 ``None`` 时覆盖全部资产；指定时去重且不考虑顺序，
        只覆盖所列资产，未知资产抛 :class:`NotFoundError`。

        本调用为只读操作：不注册候选文档、不生成/改动变更报告、不写入检索
        索引；既有的读取、比较、影响分析与检索行为保持不变。
        """
        candidate_document, label = self._resolve_comparison_inputs(
            name, baseline_version, candidate, candidate_version
        )
        summary = upgrade_mod.build_upgrade_report(
            self._registry,
            name,
            baseline_version,
            candidate_document,
            label,
            renames,
            asset_ids,
        )
        return copy.deepcopy(summary)

    # ============================================================== 变更预检
    def preview_changes(
        self, changes: Any, *, search: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """只读预检一批 Schema 变更：不注册、不生成报告、不写索引。

        ``changes`` 为 JSON 列表，每项含 ``schema``、``version``、
        ``changeType`` 及对应内容；``changeType`` 仅限 ``add_field``、
        ``remove_field``、``change_type``、``compatibility_check``、
        ``unregister_schema``。``search`` 沿用 :meth:`search` 的检索条件，
        用于限定 ``searchPreview`` 的覆盖范围。

        返回 ``accepted``、``conflicts``、``impactedItems``、
        ``searchPreview``；详见 :mod:`meta_catalog.preview`。
        """
        return preview_mod.preview_changes(self._registry, self._index, changes, search)

    def get_report(self, report_id: str) -> dict[str, Any]:
        if report_id not in self._reports:
            raise NotFoundError(
                f"变更报告 {report_id} 不存在", details={"report_id": report_id}
            )
        return copy.deepcopy(self._reports[report_id])

    def list_reports(
        self, name: str | None = None, version: str | None = None
    ) -> list[dict[str, Any]]:
        """列出报告摘要（可按 Schema 名称与版本过滤，版本匹配基线或候选）。"""
        out = []
        for rid in sorted(self._reports):
            r = self._reports[rid]
            if name is not None and r["schema"] != name:
                continue
            if version is not None and version not in (
                r["baseline_version"],
                r["candidate_version"],
            ):
                continue
            out.append(
                {
                    "report_id": r["report_id"],
                    "schema": r["schema"],
                    "baseline_version": r["baseline_version"],
                    "candidate_version": r["candidate_version"],
                    "summary": r["summary"],
                }
            )
        return out

    # ================================================================== 检索
    def search(self, keyword: str | None = None, **filters: Any) -> list[dict[str, Any]]:
        """组合检索 Schema / 资产 / 字段变更。

        可选过滤：``schema``、``version``、``field_path``、``change_kind``、
        ``compatibility``、``asset_name``、``doc_type``、``limit``。
        返回命中条目（含命中字段与命中资产摘要），排序稳定，与插入顺序无关。
        """
        return self._index.search(keyword, **filters)

    def search_page(self, keyword: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """分页检索 Schema / 资产 / 字段变更（只读，不写索引）。

        检索条件与排序口径和 :meth:`search` 完全一致：``keyword`` 与
        ``doc_type``、``schema``、``version``、``field_path``、
        ``change_kind``、``compatibility``、``asset_name``；另接受
        ``page_size``（默认 50，仅 1 到 200 的普通整数）与可选 ``cursor``。
        第一页不传 ``cursor``，后续页只传上一页返回的 ``next_cursor``。

        返回 ``items``（与 ``search`` 同条件结果逐项同构且顺序一致）、
        ``total``（调用时索引命中总数）、``page_size``、``next_cursor``
        （不透明，末页为 None）。游标绑定全部查询条件与 ``page_size``，
        任何一项变化都不得复用。参数或游标不合法时抛
        :class:`meta_catalog.errors.SearchQueryInvalid`。
        """
        return self._index.search_page(keyword, **kwargs)

    # ------------------------------------------------------------ 索引维护
    def _index_schema(self, name: str, version: str, document: Any, title: str | None) -> None:
        fields = sf.expand(document)
        root = fields.get("")
        description = ""
        if root is not None:
            d = root.schema.get("description")
            description = d if isinstance(d, str) else ""
        text_fields = {
            "name": name,
            "version": version,
            "title": title or "",
            "description": description,
            "field_paths": "\n".join(sorted(fields)),
        }
        payload = {
            "name": name,
            "version": version,
            "title": title,
            "summary": {
                "name": name,
                "version": version,
                "title": title,
                "field_count": len(fields),
            },
        }
        self._index.add_doc(
            "schema", ("schema", name, version), text_fields, payload
        )

    def _index_asset(self, asset_id: str, name: str, kind: str, refs: list[FieldRef]) -> None:
        schemas = sorted({r.schema for r in refs})
        versions = sorted({f"{r.schema}@{r.version}" for r in refs})
        text_fields = {
            "id": asset_id,
            "name": name,
            "kind": kind,
            "refs": "\n".join(f"{r.schema}@{r.version}{r.path}" for r in refs),
        }
        payload = {
            "id": asset_id,
            "name": name,
            "kind": kind,
            "schemas": schemas,
            "versions": versions,
            "summary": {
                "asset_id": asset_id,
                "name": name,
                "kind": kind,
                "ref_count": len(refs),
            },
        }
        self._index.add_doc("asset", ("asset", asset_id), text_fields, payload)

    def _index_change(self, report: dict[str, Any], change: dict[str, Any], seq: int) -> None:
        key, text_fields, payload = indexing.change_doc(report, change, seq)
        self._index.add_doc("change", key, text_fields, payload)
