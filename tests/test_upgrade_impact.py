import copy
import json
import unittest

from meta_catalog import MetaCatalog, limits
from meta_catalog.errors import (
    ImpactAnalysisTooLarge,
    NotFoundError,
    SchemaComparisonInvalid,
)


def obj(**props_and_req):
    req = props_and_req.pop("__required__", [])
    doc = {"type": "object", "properties": props_and_req}
    if req:
        doc["required"] = req
    return doc


class UpgradeImpactTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema(
            "Address",
            "1.0",
            obj(
                street={"type": "string"},
                zip={"type": "string"},
                code={"type": "string", "enum": ["a", "b"]},
            ),
        )
        self.c.register_schema(
            "Person",
            "1.0",
            obj(
                name={"type": "string"},
                address={"$ref": "Address@1.0#"},
            ),
        )
        # /street 收窄为 integer：breaking
        # /zip 仅描述变化：metadata
        # /code 放宽枚举：compatible
        self.v2 = obj(
            street={"type": "integer"},
            zip={"type": "string", "description": "邮编"},
            code={"type": "string", "enum": ["a", "b", "c"]},
        )

        self.c.register_asset(
            "svc-direct", "直接服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/street"}],
        )
        self.c.register_asset(
            "svc-trans", "传递服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/address/street"}],
        )
        self.c.register_asset(
            "svc-both", "双重命中服务", "service",
            [
                {"schema": "Address", "version": "1.0", "path": "/street"},
                {"schema": "Person", "version": "1.0", "path": "/address/street"},
            ],
        )
        self.c.register_asset(
            "svc-meta", "元数据服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/zip"}],
        )
        self.c.register_asset(
            "svc-compat", "兼容服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/code"}],
        )
        self.c.register_asset(
            "svc-multi", "多命中服务", "service",
            [
                {"schema": "Address", "version": "1.0", "path": "/street"},
                {"schema": "Address", "version": "1.0", "path": "/zip"},
            ],
        )
        self.c.register_asset(
            "svc-ok", "无关服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/name"}],
        )

    # ------------------------------------------------------------- 结构与计数
    def test_top_level_shape_and_field_order(self):
        rep = self.c.analyze_upgrade_impact(
            "Address", "1.0", self.v2, candidate_version="2.0"
        )
        self.assertEqual(
            list(rep.keys()),
            ["report_id", "schema", "baseline_version",
             "candidate_version", "summary", "assets"],
        )
        self.assertEqual(rep["schema"], "Address")
        self.assertEqual(rep["baseline_version"], "1.0")
        self.assertEqual(rep["candidate_version"], "2.0")
        self.assertEqual(
            list(rep["summary"].keys()),
            ["asset_total", "breaking_assets", "compatible_assets",
             "metadata_assets", "unaffected_assets", "changed_paths"],
        )
        self.assertTrue(json.dumps(rep, ensure_ascii=False))  # 可 JSON 序列化

    def test_summary_counts(self):
        rep = self.c.analyze_upgrade_impact("Address", "1.0", self.v2)
        self.assertEqual(
            rep["summary"],
            {
                "asset_total": 7,
                "breaking_assets": 4,   # svc-both / svc-direct / svc-multi / svc-trans
                "compatible_assets": 1,
                "metadata_assets": 1,
                "unaffected_assets": 1,
                "changed_paths": 3,     # /street /zip /code 去重
            },
        )

    def test_assets_sorted_and_contain_unaffected(self):
        rep = self.c.analyze_upgrade_impact("Address", "1.0", self.v2)
        ids = [a["asset_id"] for a in rep["assets"]]
        self.assertEqual(
            ids,
            ["svc-both", "svc-compat", "svc-direct", "svc-meta",
             "svc-multi", "svc-ok", "svc-trans"],
        )
        by_id = {a["asset_id"]: a for a in rep["assets"]}
        unaffected = by_id["svc-ok"]
        self.assertEqual(
            list(unaffected.keys()),
            ["asset_id", "name", "kind", "status", "changes"],
        )
        self.assertEqual(unaffected["status"], "unaffected")
        self.assertEqual(unaffected["changes"], [])
        self.assertEqual(unaffected["name"], "无关服务")
        self.assertEqual(unaffected["kind"], "service")

    def test_statuses_follow_severity(self):
        rep = self.c.analyze_upgrade_impact("Address", "1.0", self.v2)
        by_id = {a["asset_id"]: a for a in rep["assets"]}
        self.assertEqual(by_id["svc-both"]["status"], "breaking")
        self.assertEqual(by_id["svc-direct"]["status"], "breaking")
        self.assertEqual(by_id["svc-trans"]["status"], "breaking")
        self.assertEqual(by_id["svc-compat"]["status"], "compatible")
        self.assertEqual(by_id["svc-meta"]["status"], "metadata")
        # breaking 严重度高于 metadata
        self.assertEqual(by_id["svc-multi"]["status"], "breaking")

    # ----------------------------------------------------------- impact_kind
    def test_impact_kinds_direct_transitive_both(self):
        rep = self.c.analyze_upgrade_impact("Address", "1.0", self.v2)
        by_id = {a["asset_id"]: a for a in rep["assets"]}

        (d,) = by_id["svc-direct"]["changes"]
        self.assertEqual(d["path"], "/street")
        self.assertEqual(d["impact_kind"], "direct")

        (t,) = by_id["svc-trans"]["changes"]
        self.assertEqual(t["path"], "/street")
        self.assertEqual(t["impact_kind"], "transitive")

        (b,) = by_id["svc-both"]["changes"]  # 同一资产同一变更只有一条
        self.assertEqual(b["path"], "/street")
        self.assertEqual(b["impact_kind"], "both")

        (m,) = by_id["svc-meta"]["changes"]
        self.assertEqual(m["path"], "/zip")
        self.assertEqual(m["impact_kind"], "direct")

        (compat,) = by_id["svc-compat"]["changes"]
        self.assertEqual(compat["path"], "/code")
        self.assertEqual(compat["impact_kind"], "direct")

    def test_change_entries_keep_report_fields_and_order(self):
        rep = self.c.analyze_upgrade_impact("Address", "1.0", self.v2)
        by_id = {a["asset_id"]: a for a in rep["assets"]}
        changes = by_id["svc-multi"]["changes"]
        # 稳定排序：/street 在 /zip 之前（与报告排序一致）
        self.assertEqual([ch["path"] for ch in changes], ["/street", "/zip"])
        for ch in changes:
            self.assertEqual(
                list(ch.keys()),
                ["path", "old_path", "new_path", "old_summary", "new_summary",
                 "change_kind", "compatibility", "direct_assets",
                 "transitive_assets", "impact_kind"],
            )
            self.assertIn(ch["impact_kind"], {"direct", "transitive", "both"})
        self.assertEqual(changes[0]["compatibility"], "breaking")
        self.assertEqual(changes[1]["compatibility"], "metadata")

    # -------------------------------------------------------------- asset_ids
    def test_asset_ids_dedup_order_independent_and_scopes_summary(self):
        rep = self.c.analyze_upgrade_impact(
            "Address", "1.0", self.v2,
            asset_ids=["svc-ok", "svc-both", "svc-both"],
        )
        self.assertEqual([a["asset_id"] for a in rep["assets"]],
                         ["svc-both", "svc-ok"])
        self.assertEqual(
            rep["summary"],
            {
                "asset_total": 2,
                "breaking_assets": 1,
                "compatible_assets": 0,
                "metadata_assets": 0,
                "unaffected_assets": 1,
                "changed_paths": 1,  # 仅入选资产命中的 /street
            },
        )

    def test_empty_asset_ids_covers_nothing(self):
        rep = self.c.analyze_upgrade_impact(
            "Address", "1.0", self.v2, asset_ids=[]
        )
        self.assertEqual(rep["assets"], [])
        self.assertEqual(rep["summary"]["asset_total"], 0)
        self.assertEqual(rep["summary"]["changed_paths"], 0)

    def test_unknown_asset_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.c.analyze_upgrade_impact(
                "Address", "1.0", self.v2, asset_ids=["svc-missing"]
            )

    # --------------------------------------------------------- report_id 一致
    def test_report_id_matches_compare_schemas(self):
        impact = self.c.analyze_upgrade_impact(
            "Address", "1.0", self.v2, candidate_version="2.0",
            renames=[],
        )
        # analyze 不应落库
        self.assertEqual(self.c.list_reports(), [])
        compared = self.c.compare_schemas(
            "Address", "1.0", self.v2, candidate_version="2.0",
            renames=[],
        )
        self.assertEqual(impact["report_id"], compared["report_id"])

    def test_report_id_matches_with_registered_candidate_version(self):
        self.c.register_schema("Address", "2.0", self.v2)
        impact = self.c.analyze_upgrade_impact(
            "Address", "1.0", candidate_version="2.0"
        )
        compared = self.c.compare_schemas(
            "Address", "1.0", candidate_version="2.0"
        )
        self.assertEqual(impact["report_id"], compared["report_id"])

    # --------------------------------------------------------------- 不可变性
    def test_does_not_mutate_registry_reports_or_index(self):
        versions_before = self.c.list_versions("Address")
        search_before = self.c.search("street")
        n_reports = len(self.c.list_reports())

        rep = self.c.analyze_upgrade_impact(
            "Address", "1.0", self.v2, candidate_version="2.0"
        )

        # 候选文档未被注册
        self.assertEqual(self.c.list_versions("Address"), versions_before)
        # 报告未入库
        self.assertEqual(len(self.c.list_reports()), n_reports)
        with self.assertRaises(NotFoundError):
            self.c.get_report(rep["report_id"])
        # 索引未变化
        self.assertEqual(self.c.search("street"), search_before)

    def test_result_is_a_deep_copy(self):
        r1 = self.c.analyze_upgrade_impact("Address", "1.0", self.v2)
        r1["assets"][0]["changes"].clear()
        r1["summary"]["asset_total"] = -1
        r2 = self.c.analyze_upgrade_impact("Address", "1.0", self.v2)
        self.assertEqual(len(r2["assets"][0]["changes"]), 1)
        self.assertEqual(r2["summary"]["asset_total"], 7)

    def test_deterministic_across_calls(self):
        r1 = self.c.analyze_upgrade_impact("Address", "1.0", self.v2)
        r2 = self.c.analyze_upgrade_impact(
            "Address", "1.0", copy.deepcopy(self.v2)
        )
        self.assertEqual(r1, r2)

    # ----------------------------------------------------------------- 错误
    def test_invalid_candidate_document(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.analyze_upgrade_impact(
                "Address", "1.0", ["not", "a", "schema"]
            )

    def test_candidate_version_not_found(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.analyze_upgrade_impact(
                "Address", "1.0", candidate_version="9.9"
            )
        self.assertEqual(cm.exception.details["reason"], "version_not_found")

    def test_baseline_version_not_found(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.analyze_upgrade_impact("Address", "9.9", self.v2)
        self.assertEqual(cm.exception.details["reason"], "version_not_found")

    def test_missing_candidate(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.analyze_upgrade_impact("Address", "1.0")
        self.assertEqual(cm.exception.details["reason"], "candidate_missing")

    def test_invalid_rename(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.analyze_upgrade_impact(
                "Address", "1.0", self.v2,
                renames=[{"from": "/missing", "to": "/nowhere"}],
            )

    def test_too_many_changes_raises_too_large(self):
        old = limits.MAX_CHANGES
        limits.MAX_CHANGES = 1
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                self.c.analyze_upgrade_impact("Address", "1.0", self.v2)
            self.assertEqual(cm.exception.details["reason"], "changes_exceeded")
        finally:
            limits.MAX_CHANGES = old


if __name__ == "__main__":
    unittest.main()
