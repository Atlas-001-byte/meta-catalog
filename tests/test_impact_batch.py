import unittest

from meta_catalog import MetaCatalog
from meta_catalog.errors import (
    ImpactAnalysisInvalid,
    NotFoundError,
)


def schema(**props):
    return {"type": "object", "properties": props}


def request(**overrides):
    req = {
        "schema": "Address",
        "version": "1.0",
        "paths": ["/street"],
        "mode": "any",
    }
    req.update(overrides)
    return req


class ImpactBatchTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema(
            "Address",
            "1.0",
            schema(street={"type": "string"}, zip={"type": "string"}),
        )
        self.c.register_schema(
            "Person",
            "1.0",
            schema(
                name={"type": "string"},
                address={"$ref": "Address@1.0#"},
            ),
        )
        self.c.register_schema(
            "Company",
            "1.0",
            schema(contact={"$ref": "Person@1.0#/properties/address"}),
        )
        self.c.register_asset(
            "svc-mail", "邮寄服务", "service",
            [
                {"schema": "Address", "version": "1.0", "path": "/street"},
                {"schema": "Address", "version": "1.0", "path": "/zip"},
            ],
        )
        self.c.register_asset(
            "svc-zip", "邮编服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/zip"}],
        )
        self.c.register_asset(
            "svc-billing", "账单服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/address/street"}],
        )
        self.c.register_asset(
            "svc-company", "公司服务", "service",
            [{"schema": "Company", "version": "1.0", "path": "/contact/street"}],
        )

    # ------------------------------------------------------------ 基本命中
    def test_any_mode_single_field(self):
        res = self.c.analyze_impact_batch(request())
        self.assertEqual(res["schema"], "Address")
        self.assertEqual(res["version"], "1.0")
        self.assertEqual(res["paths"], ["/street"])
        self.assertEqual(res["mode"], "any")
        ids = [a["asset_id"] for a in res["assets"]]
        self.assertEqual(ids, ["svc-billing", "svc-company", "svc-mail"])
        self.assertEqual(
            res["summary"],
            {
                "candidate_assets": 4,
                "selected_assets": 3,
                "field_hits": {"/street": 3},
                "matched_assets": 3,
            },
        )

    def test_any_mode_multiple_fields(self):
        res = self.c.analyze_impact_batch(request(paths=["/street", "/zip"]))
        ids = [a["asset_id"] for a in res["assets"]]
        self.assertEqual(ids, ["svc-billing", "svc-company", "svc-mail", "svc-zip"])
        self.assertEqual(
            res["summary"]["field_hits"], {"/street": 3, "/zip": 2}
        )
        self.assertEqual(res["summary"]["selected_assets"], 4)
        self.assertEqual(res["summary"]["matched_assets"], 4)

    def test_all_mode_requires_every_field(self):
        res = self.c.analyze_impact_batch(
            request(paths=["/street", "/zip"], mode="all")
        )
        self.assertEqual([a["asset_id"] for a in res["assets"]], ["svc-mail"])
        self.assertEqual(res["summary"]["selected_assets"], 4)
        self.assertEqual(res["summary"]["matched_assets"], 1)

    def test_all_mode_field_without_hits_yields_empty(self):
        res = self.c.analyze_impact_batch(
            request(paths=["/street", "/zip"], mode="all",
                    asset_ids=["svc-billing", "svc-company"])
        )
        self.assertEqual(res["assets"], [])
        self.assertEqual(res["summary"]["matched_assets"], 0)

    def test_any_mode_no_hits_yields_empty(self):
        res = self.c.analyze_impact_batch(
            request(paths=["/street"], asset_ids=["svc-zip"])
        )
        self.assertEqual(res["assets"], [])
        self.assertEqual(res["summary"]["selected_assets"], 0)
        self.assertEqual(res["summary"]["field_hits"], {"/street": 0})

    # ------------------------------------------------------------ 去重与顺序
    def test_paths_deduplicated_by_first_occurrence(self):
        res = self.c.analyze_impact_batch(
            request(paths=["/zip", "/street", "/zip", "/street"])
        )
        self.assertEqual(res["paths"], ["/zip", "/street"])
        self.assertEqual(
            list(res["summary"]["field_hits"]), ["/zip", "/street"]
        )

    def test_matched_fields_follow_input_order(self):
        res = self.c.analyze_impact_batch(request(paths=["/zip", "/street"]))
        mail = next(a for a in res["assets"] if a["asset_id"] == "svc-mail")
        self.assertEqual(mail["matched_fields"], ["/zip", "/street"])
        self.assertEqual(list(mail["fields"]), ["/zip", "/street"])

    def test_assets_sorted_by_asset_id(self):
        res = self.c.analyze_impact_batch(request(paths=["/street", "/zip"]))
        ids = [a["asset_id"] for a in res["assets"]]
        self.assertEqual(ids, sorted(ids))

    # ------------------------------------------------------------ 字段明细
    def test_direct_field_detail(self):
        res = self.c.analyze_impact_batch(request())
        mail = next(a for a in res["assets"] if a["asset_id"] == "svc-mail")
        self.assertEqual(mail["impact_kind"], "direct")
        detail = mail["fields"]["/street"]
        self.assertEqual(detail["impact_kind"], "direct")
        self.assertEqual(
            detail["direct"],
            {"asset_id": "svc-mail", "name": "邮寄服务", "kind": "service"},
        )
        self.assertEqual(detail["transitive"], [])

    def test_transitive_field_detail(self):
        res = self.c.analyze_impact_batch(request())
        billing = next(a for a in res["assets"] if a["asset_id"] == "svc-billing")
        self.assertEqual(billing["impact_kind"], "transitive")
        detail = billing["fields"]["/street"]
        self.assertEqual(detail["impact_kind"], "transitive")
        self.assertIsNone(detail["direct"])
        self.assertEqual(
            detail["transitive"],
            [
                {
                    "schema": "Person",
                    "version": "1.0",
                    "path": "/address/street",
                    "matched_paths": ["/street"],
                }
            ],
        )

    def test_both_impact_kind(self):
        self.c.register_asset(
            "svc-both", "双重服务", "service",
            [
                {"schema": "Address", "version": "1.0", "path": "/street"},
                {"schema": "Person", "version": "1.0", "path": "/address/street"},
            ],
        )
        res = self.c.analyze_impact_batch(request())
        both = next(a for a in res["assets"] if a["asset_id"] == "svc-both")
        self.assertEqual(both["impact_kind"], "both")
        self.assertEqual(both["fields"]["/street"]["impact_kind"], "both")

    def test_root_field_inclusion(self):
        # 根字段沿用现有包含关系：经其他 Schema 传递到达 /street 的资产命中根；
        # 与单字段 analyze_impact 同口径（同 Schema 子路径引用不计入根命中）。
        res = self.c.analyze_impact_batch(request(paths=[""]))
        ids = {a["asset_id"] for a in res["assets"]}
        self.assertIn("svc-billing", ids)
        self.assertIn("svc-company", ids)
        single = self.c.analyze_impact("Address", "1.0", "")
        single_ids = {a["asset_id"] for a in single["direct_assets"]}
        single_ids |= {a["asset_id"] for a in single["transitive_assets"]}
        self.assertEqual(ids, single_ids)

    def test_parent_child_inclusion(self):
        # 引用整个 /address 子树的资产，对 /street 查询构成传递命中。
        self.c.register_asset(
            "svc-whole", "整对象服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/address"}],
        )
        res = self.c.analyze_impact_batch(request())
        whole = next(a for a in res["assets"] if a["asset_id"] == "svc-whole")
        self.assertEqual(whole["impact_kind"], "transitive")
        self.assertEqual(
            whole["fields"]["/street"]["transitive"],
            [
                {
                    "schema": "Person",
                    "version": "1.0",
                    "path": "/address",
                    "matched_paths": [""],
                }
            ],
        )

    # ------------------------------------------------------------ 资产选择
    def test_asset_ids_restrict_candidates(self):
        res = self.c.analyze_impact_batch(
            request(asset_ids=["svc-company", "svc-mail", "svc-mail"])
        )
        self.assertEqual(
            [a["asset_id"] for a in res["assets"]], ["svc-company", "svc-mail"]
        )
        self.assertEqual(res["summary"]["candidate_assets"], 2)

    def test_asset_ids_empty_selects_nothing(self):
        res = self.c.analyze_impact_batch(
            request(paths=["/street", "/zip"], asset_ids=[])
        )
        self.assertEqual(res["assets"], [])
        self.assertEqual(
            res["summary"],
            {
                "candidate_assets": 0,
                "selected_assets": 0,
                "field_hits": {"/street": 0, "/zip": 0},
                "matched_assets": 0,
            },
        )

    # ------------------------------------------------------------ 非法请求
    def test_request_not_dict(self):
        for bad in (None, "x", ["/street"], 1):
            with self.assertRaises(ImpactAnalysisInvalid):
                self.c.analyze_impact_batch(bad)

    def test_paths_not_list_or_non_string(self):
        with self.assertRaises(ImpactAnalysisInvalid):
            self.c.analyze_impact_batch(request(paths="/street"))
        with self.assertRaises(ImpactAnalysisInvalid):
            self.c.analyze_impact_batch(request(paths=["/street", 1]))
        with self.assertRaises(ImpactAnalysisInvalid):
            self.c.analyze_impact_batch(request(paths=None))

    def test_empty_paths_after_dedup(self):
        with self.assertRaises(ImpactAnalysisInvalid):
            self.c.analyze_impact_batch(request(paths=[]))

    def test_invalid_mode(self):
        for bad in ("both", "", None, 1):
            with self.assertRaises(ImpactAnalysisInvalid):
                self.c.analyze_impact_batch(request(mode=bad))

    def test_invalid_asset_ids(self):
        with self.assertRaises(ImpactAnalysisInvalid):
            self.c.analyze_impact_batch(request(asset_ids="svc-mail"))
        with self.assertRaises(ImpactAnalysisInvalid):
            self.c.analyze_impact_batch(request(asset_ids=["svc-mail", ""]))
        with self.assertRaises(ImpactAnalysisInvalid):
            self.c.analyze_impact_batch(request(asset_ids=[1]))

    def test_invalid_pointer(self):
        with self.assertRaises(ImpactAnalysisInvalid):
            self.c.analyze_impact_batch(request(paths=["street"]))

    def test_invalid_error_code(self):
        try:
            self.c.analyze_impact_batch(request(paths=[]))
        except ImpactAnalysisInvalid as exc:
            self.assertEqual(exc.code, "ImpactAnalysisInvalid")
        else:
            self.fail("expected ImpactAnalysisInvalid")

    # ------------------------------------------------------------ 不存在
    def test_unknown_schema_or_version(self):
        with self.assertRaises(NotFoundError):
            self.c.analyze_impact_batch(request(schema="Nope"))
        with self.assertRaises(NotFoundError):
            self.c.analyze_impact_batch(request(version="9.9"))

    def test_unknown_field(self):
        with self.assertRaises(NotFoundError):
            self.c.analyze_impact_batch(request(paths=["/nope"]))

    def test_unknown_asset(self):
        with self.assertRaises(NotFoundError):
            self.c.analyze_impact_batch(request(asset_ids=["svc-unknown"]))

    # ------------------------------------------------------------ 只读与确定性
    def test_deterministic_and_independent_results(self):
        req = request(paths=["/street", "/zip"])
        res1 = self.c.analyze_impact_batch(req)
        res2 = self.c.analyze_impact_batch(req)
        self.assertEqual(res1, res2)
        self.assertIsNot(res1, res2)
        # 修改返回值不影响后续调用（不共享可变引用）。
        res1["assets"][0]["fields"].clear()
        res1["summary"]["matched_assets"] = -1
        res3 = self.c.analyze_impact_batch(req)
        self.assertEqual(res3, res2)

    def test_read_only(self):
        before_search = self.c.search()
        before_reports = self.c.list_reports()
        before_schema = self.c.get_schema("Address", "1.0")
        before_impact = self.c.analyze_impact("Address", "1.0", "/street")
        self.c.analyze_impact_batch(request(paths=["/street", "/zip"]))
        self.assertEqual(self.c.search(), before_search)
        self.assertEqual(self.c.list_reports(), before_reports)
        self.assertEqual(self.c.get_schema("Address", "1.0"), before_schema)
        self.assertEqual(
            self.c.analyze_impact("Address", "1.0", "/street"), before_impact
        )

    def test_validation_failure_returns_no_partial_result(self):
        # 第二个字段不存在：整体抛错，不返回部分命中。
        with self.assertRaises(NotFoundError):
            self.c.analyze_impact_batch(request(paths=["/street", "/nope"]))


if __name__ == "__main__":
    unittest.main()
