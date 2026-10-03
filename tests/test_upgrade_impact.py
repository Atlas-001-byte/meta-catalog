import copy
import json
import unittest

from meta_catalog import MetaCatalog, limits
from meta_catalog.errors import (
    ImpactAnalysisTooLarge,
    NotFoundError,
    SchemaComparisonInvalid,
)


def schema(**props):
    return {"type": "object", "properties": props}


# Address 1.0 -> 2.0：street 仅标题变化（metadata），zip 收窄为 integer
# （breaking），新增可选 note（compatible，新字段无影响资产）。
V2_ADDRESS = {
    "type": "object",
    "properties": {
        "street": {"type": "string", "title": "街道"},
        "zip": {"type": "integer"},
        "note": {"type": "string"},
    },
}


def build_catalog():
    c = MetaCatalog()
    c.register_schema(
        "Address", "1.0",
        schema(street={"type": "string"}, zip={"type": "string"}),
    )
    c.register_schema(
        "Person", "1.0",
        schema(name={"type": "string"}, address={"$ref": "Address@1.0#"}),
    )
    c.register_schema(
        "Company", "1.0",
        schema(contact={"$ref": "Person@1.0#/properties/address"}),
    )
    c.register_schema("Other", "1.0", schema(id={"type": "string"}))

    # 直接命中 street（metadata）
    c.register_asset(
        "svc-mail", "邮寄服务", "service",
        [{"schema": "Address", "version": "1.0", "path": "/street"}],
    )
    # 直接命中 zip（breaking）
    c.register_asset(
        "svc-zip", "邮编服务", "service",
        [{"schema": "Address", "version": "1.0", "path": "/zip"}],
    )
    # 传递命中 street（一跳）
    c.register_asset(
        "svc-billing", "账单服务", "service",
        [{"schema": "Person", "version": "1.0", "path": "/address/street"}],
    )
    # 传递命中 street（两跳）
    c.register_asset(
        "svc-company", "公司服务", "service",
        [{"schema": "Company", "version": "1.0", "path": "/contact/street"}],
    )
    # street 同时直接与传递命中，且直接命中 zip -> status 取 breaking
    c.register_asset(
        "svc-both", "双重服务", "service",
        [
            {"schema": "Address", "version": "1.0", "path": "/street"},
            {"schema": "Person", "version": "1.0", "path": "/address/street"},
            {"schema": "Address", "version": "1.0", "path": "/zip"},
        ],
    )
    # 与本次升级完全无关
    c.register_asset(
        "svc-idle", "空闲服务", "service",
        [{"schema": "Other", "version": "1.0", "path": "/id"}],
    )
    return c


class UpgradeImpactShapeTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()
        self.res = self.c.analyze_upgrade_impact(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )

    def test_top_level_shape_and_order(self):
        self.assertEqual(
            list(self.res.keys()),
            [
                "report_id",
                "schema",
                "baseline_version",
                "candidate_version",
                "summary",
                "assets",
            ],
        )
        self.assertEqual(self.res["schema"], "Address")
        self.assertEqual(self.res["baseline_version"], "1.0")
        self.assertEqual(self.res["candidate_version"], "2.0")

    def test_summary_counts(self):
        self.assertEqual(
            self.res["summary"],
            {
                "asset_total": 6,
                "breaking_assets": 2,       # svc-zip, svc-both
                "compatible_assets": 0,
                "metadata_assets": 3,       # svc-mail, svc-billing, svc-company
                "unaffected_assets": 1,     # svc-idle
                # 入选资产只命中 street 与 zip；新增 note 无影响资产
                "changed_paths": 2,
            },
        )

    def test_assets_sorted_and_entry_shape(self):
        ids = [a["asset_id"] for a in self.res["assets"]]
        self.assertEqual(
            ids,
            ["svc-billing", "svc-both", "svc-company", "svc-idle", "svc-mail", "svc-zip"],
        )
        for a in self.res["assets"]:
            self.assertEqual(
                list(a.keys()), ["asset_id", "name", "kind", "status", "changes"]
            )

    def test_statuses_and_impact_kinds(self):
        by_id = {a["asset_id"]: a for a in self.res["assets"]}
        self.assertEqual(by_id["svc-zip"]["status"], "breaking")
        self.assertEqual(by_id["svc-both"]["status"], "breaking")
        self.assertEqual(by_id["svc-mail"]["status"], "metadata")
        self.assertEqual(by_id["svc-billing"]["status"], "metadata")
        self.assertEqual(by_id["svc-company"]["status"], "metadata")
        self.assertEqual(by_id["svc-idle"]["status"], "unaffected")
        self.assertEqual(by_id["svc-idle"]["changes"], [])

        kinds_mail = {ch["path"]: ch["impact_kind"] for ch in by_id["svc-mail"]["changes"]}
        self.assertEqual(kinds_mail, {"/street": "direct"})

        kinds_billing = {ch["path"]: ch["impact_kind"] for ch in by_id["svc-billing"]["changes"]}
        self.assertEqual(kinds_billing, {"/street": "transitive"})

        kinds_company = {ch["path"]: ch["impact_kind"] for ch in by_id["svc-company"]["changes"]}
        self.assertEqual(kinds_company, {"/street": "transitive"})

        kinds_both = {ch["path"]: ch["impact_kind"] for ch in by_id["svc-both"]["changes"]}
        self.assertEqual(kinds_both, {"/street": "both", "/zip": "direct"})

    def test_changes_keep_report_fields_plus_impact_kind(self):
        report = self.c.compare_schemas(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )
        report_by_path = {ch["path"]: ch for ch in report["changes"]}
        for a in self.res["assets"]:
            for ch in a["changes"]:
                base = report_by_path[ch["path"]]
                self.assertEqual(
                    list(ch.keys()), list(base.keys()) + ["impact_kind"]
                )
                for key in base:
                    self.assertEqual(ch[key], base[key])

    def test_change_order_follows_report_order(self):
        report = self.c.compare_schemas(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )
        order = [ch["path"] for ch in report["changes"]]
        both = next(a for a in self.res["assets"] if a["asset_id"] == "svc-both")
        got = [ch["path"] for ch in both["changes"]]
        self.assertEqual(got, [p for p in order if p in got])

    def test_json_serializable(self):
        json.dumps(self.res, ensure_ascii=False)

    def test_deterministic_across_calls(self):
        other = build_catalog().analyze_upgrade_impact(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )
        self.assertEqual(
            json.dumps(self.res, sort_keys=True, ensure_ascii=False),
            json.dumps(other, sort_keys=True, ensure_ascii=False),
        )
        # 字段（键）顺序也必须一致
        self.assertEqual(
            [list(a.keys()) for a in self.res["assets"]],
            [list(a.keys()) for a in other["assets"]],
        )

    def test_report_id_matches_compare_schemas(self):
        report = self.c.compare_schemas(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )
        self.assertEqual(self.res["report_id"], report["report_id"])


class UpgradeImpactSelectionTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()
        self.kwargs = dict(candidate=V2_ADDRESS, candidate_version="2.0")

    def test_asset_ids_dedup_order_insensitive(self):
        r1 = self.c.analyze_upgrade_impact(
            "Address", "1.0", V2_ADDRESS,
            candidate_version="2.0",
            asset_ids=["svc-idle", "svc-mail", "svc-idle"],
        )
        r2 = self.c.analyze_upgrade_impact(
            "Address", "1.0", V2_ADDRESS,
            candidate_version="2.0",
            asset_ids=["svc-mail", "svc-idle"],
        )
        self.assertEqual(r1, r2)
        self.assertEqual([a["asset_id"] for a in r1["assets"]], ["svc-idle", "svc-mail"])
        self.assertEqual(r1["summary"]["asset_total"], 2)
        self.assertEqual(r1["summary"]["metadata_assets"], 1)
        self.assertEqual(r1["summary"]["unaffected_assets"], 1)
        # changed_paths 只统计入选资产的命中
        self.assertEqual(r1["summary"]["changed_paths"], 1)

    def test_empty_asset_ids(self):
        r = self.c.analyze_upgrade_impact(
            "Address", "1.0", V2_ADDRESS,
            candidate_version="2.0", asset_ids=[],
        )
        self.assertEqual(r["assets"], [])
        self.assertEqual(r["summary"]["asset_total"], 0)
        self.assertEqual(r["summary"]["changed_paths"], 0)

    def test_unknown_asset_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.c.analyze_upgrade_impact(
                "Address", "1.0", V2_ADDRESS,
                candidate_version="2.0", asset_ids=["svc-mail", "nope"],
            )

    def test_subset_does_not_leak_other_assets_into_changed_paths(self):
        r = self.c.analyze_upgrade_impact(
            "Address", "1.0", V2_ADDRESS,
            candidate_version="2.0", asset_ids=["svc-idle"],
        )
        self.assertEqual(r["summary"]["changed_paths"], 0)
        self.assertEqual(r["assets"][0]["status"], "unaffected")


class UpgradeImpactValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_invalid_candidate_document(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.analyze_upgrade_impact("Address", "1.0", ["not", "a", "schema"])

    def test_missing_candidate(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.analyze_upgrade_impact("Address", "1.0")
        self.assertEqual(cm.exception.details["reason"], "candidate_missing")

    def test_baseline_version_not_found(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.analyze_upgrade_impact(
                "Address", "9.9", V2_ADDRESS, candidate_version="2.0"
            )
        self.assertEqual(cm.exception.details["reason"], "version_not_found")

    def test_candidate_version_not_found(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.analyze_upgrade_impact(
                "Address", "1.0", candidate_version="9.9"
            )
        self.assertEqual(cm.exception.details["reason"], "version_not_found")

    def test_invalid_rename(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.analyze_upgrade_impact(
                "Address", "1.0", V2_ADDRESS, candidate_version="2.0",
                renames=[{"from": "/missing", "to": "/note"}],
            )
        self.assertEqual(cm.exception.details["reason"], "rename_from_not_found")

    def test_registered_candidate_version_supported(self):
        self.c.register_schema("Address", "2.0", V2_ADDRESS)
        r = self.c.analyze_upgrade_impact("Address", "1.0", candidate_version="2.0")
        self.assertEqual(r["candidate_version"], "2.0")

    def test_too_many_changes_raises_too_large(self):
        old = limits.MAX_CHANGES
        limits.MAX_CHANGES = 1
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                self.c.analyze_upgrade_impact(
                    "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
                )
            self.assertEqual(cm.exception.details["reason"], "changes_exceeded")
        finally:
            limits.MAX_CHANGES = old


class UpgradeImpactNoSideEffectsTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_does_not_persist_report_or_touch_index(self):
        before_reports = self.c.list_reports()
        before_all = self.c.search()
        res = self.c.analyze_upgrade_impact(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )
        # 报告未入库
        self.assertEqual(self.c.list_reports(), before_reports)
        with self.assertRaises(NotFoundError):
            self.c.get_report(res["report_id"])
        # 索引无变化（没有 change 文档新进入）
        self.assertEqual(self.c.search(), before_all)
        # 注册内容不变：内联候选未注册
        self.assertEqual(self.c.list_versions("Address"), ["1.0"])

        # 随后同输入 compare_schemas 得到相同 report_id，且报告可读取
        report = self.c.compare_schemas(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )
        self.assertEqual(report["report_id"], res["report_id"])
        self.assertEqual(self.c.get_report(res["report_id"])["report_id"], res["report_id"])

    def test_result_is_decoupled_from_catalog_state(self):
        res = self.c.analyze_upgrade_impact(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )
        snapshot = copy.deepcopy(res)
        # 再跑一次 compare 入库，不影响先前返回值结构
        self.c.compare_schemas(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )
        again = self.c.analyze_upgrade_impact(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )
        self.assertEqual(res, snapshot)
        self.assertEqual(again, snapshot)

    def test_existing_apis_unaffected(self):
        self.c.analyze_upgrade_impact(
            "Address", "1.0", V2_ADDRESS, candidate_version="2.0"
        )
        # 既有读取 / 影响分析行为不变
        imp = self.c.analyze_impact("Address", "1.0", "/street")
        self.assertEqual(
            [a["asset_id"] for a in imp["direct_assets"]],
            ["svc-both", "svc-mail"],
        )
        self.assertEqual(
            {a["asset_id"] for a in imp["transitive_assets"]},
            {"svc-billing", "svc-both", "svc-company"},
        )
        self.assertEqual(
            self.c.get_asset("svc-mail")["asset_id"], "svc-mail"
        )


if __name__ == "__main__":
    unittest.main()
