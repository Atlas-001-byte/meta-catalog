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


class ReferenceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        # 基线：/street 保留、/zip 改名为 /zipcode、/code 删除、
        # /geo 子树改名为 /location（/geo/lat 保留、/geo/lng 删除）。
        self.c.register_schema(
            "Address",
            "1.0",
            obj(
                street={"type": "string"},
                zip={"type": "string"},
                code={"type": "string"},
                geo=obj(lat={"type": "number"}, lng={"type": "number"}),
            ),
        )
        self.c.register_schema(
            "Address",
            "2.0",
            obj(
                street={"type": "string"},
                zipcode={"type": "string"},
                location=obj(lat={"type": "number"}),
            ),
        )
        # 跨 Schema 引用来源：根引用与深字段引用。
        self.c.register_schema(
            "Person",
            "1.0",
            obj(
                name={"type": "string"},
                address={"$ref": "Address@1.0#"},
                home={"$ref": "Address@1.0#/properties/street"},
            ),
        )
        self.c.register_schema(
            "Order",
            "1.0",
            obj(
                shipping={"$ref": "Address@1.0#/properties/zip"},
                billing={"$ref": "Address@1.0#/properties/code"},
            ),
        )
        # 前向引用：目标字段在基线版本中不存在（baseline_target_missing）。
        self.c.register_schema(
            "Legacy",
            "1.0",
            obj(note={"$ref": "Forward@1.0#/properties/later"}),
        )
        self.c.register_schema("Forward", "1.0", obj(other={"type": "string"}))
        self.c.register_schema("Forward", "2.0", obj(other={"type": "string"}))

        self.renames = [
            {"from": "/zip", "to": "/zipcode"},
            {"from": "/geo", "to": "/location"},
        ]

        self.c.register_asset(
            "svc-street", "街道服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/street"}],
        )
        self.c.register_asset(
            "svc-zip", "邮编服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/zip"}],
        )
        self.c.register_asset(
            "svc-code", "编码服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/code"}],
        )
        self.c.register_asset(
            "svc-geo", "地理服务", "service",
            [
                {"schema": "Address", "version": "1.0", "path": "/geo"},
                {"schema": "Address", "version": "1.0", "path": "/geo/lat"},
                {"schema": "Address", "version": "1.0", "path": "/geo/lng"},
            ],
        )
        self.c.register_asset(
            "svc-other", "无关服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/name"}],
        )

    def plan(self, **kwargs):
        args = dict(renames=self.renames)
        args.update(kwargs)
        return self.c.plan_reference_migration("Address", "1.0", "2.0", **args)

    # ------------------------------------------------------------- 结构
    def test_top_level_shape(self):
        plan = self.plan()
        self.assertEqual(
            list(plan.keys()),
            ["schema", "baseline_version", "candidate_version",
             "renames", "summary", "references"],
        )
        self.assertEqual(plan["schema"], "Address")
        self.assertEqual(plan["baseline_version"], "1.0")
        self.assertEqual(plan["candidate_version"], "2.0")
        self.assertEqual(
            plan["renames"],
            [{"from": "/geo", "to": "/location"},
             {"from": "/zip", "to": "/zipcode"}],
        )
        self.assertEqual(
            list(plan["summary"].keys()),
            ["total", "source_schemas", "source_assets",
             "ready", "renamed", "broken"],
        )
        json.dumps(plan, ensure_ascii=False)  # 可 JSON 序列化

    def test_summary_counts(self):
        summary = self.plan()["summary"]
        # 来源 Schema：Person@1.0（2 条）、Order@1.0（2 条）
        # 来源资产：svc-street / svc-zip / svc-code / svc-geo（3 条）
        self.assertEqual(
            summary,
            {
                "total": 4 + 1 + 1 + 1 + 3,
                "source_schemas": 2,
                "source_assets": 4,
                # ready：Person 根引用、Person /street、svc-street
                "ready": 3,
                # renamed：Order /zip、svc-zip、svc-geo 的 /geo 与 /geo/lat
                "renamed": 4,
                # broken：Order /code、svc-code、svc-geo 的 /geo/lng
                "broken": 3,
            },
        )

    # ----------------------------------------------------------- 引用条目
    def test_reference_entry_shape_and_statuses(self):
        plan = self.plan()
        for ref in plan["references"]:
            self.assertEqual(
                list(ref.keys()),
                ["source", "target_schema", "target_path",
                 "suggested_version", "suggested_path", "status", "reason"],
            )
            self.assertEqual(ref["target_schema"], "Address")
            if ref["status"] == "broken":
                self.assertIsNone(ref["suggested_version"])
                self.assertIsNone(ref["suggested_path"])
            else:
                self.assertEqual(ref["suggested_version"], "2.0")

    def test_schema_sources(self):
        plan = self.plan()
        by_loc = {
            (r["source"].get("schema"), r["source"].get("path"),
             r["target_path"]): r
            for r in plan["references"]
            if r["source"]["type"] == "schema"
        }
        root = by_loc[("Person", "/address", "")]
        self.assertEqual(root["status"], "ready")
        self.assertEqual(root["reason"], "path_unchanged")
        self.assertEqual(root["suggested_path"], "")

        street = by_loc[("Person", "/home", "/street")]
        self.assertEqual(street["status"], "ready")
        self.assertEqual(street["suggested_path"], "/street")

        zip_ref = by_loc[("Order", "/shipping", "/zip")]
        self.assertEqual(zip_ref["status"], "renamed")
        self.assertEqual(zip_ref["reason"], "path_renamed")
        self.assertEqual(zip_ref["suggested_path"], "/zipcode")

        code = by_loc[("Order", "/billing", "/code")]
        self.assertEqual(code["status"], "broken")
        self.assertEqual(code["reason"], "target_deleted")

    def test_asset_sources_and_deep_suffix(self):
        plan = self.plan()
        by_loc = {
            (r["source"].get("asset_id"), r["target_path"]): r
            for r in plan["references"]
            if r["source"]["type"] == "asset"
        }
        self.assertEqual(by_loc[("svc-street", "/street")]["status"], "ready")
        self.assertEqual(
            by_loc[("svc-zip", "/zip")]["suggested_path"], "/zipcode"
        )
        # 深层引用保留后缀
        self.assertEqual(
            by_loc[("svc-geo", "/geo")]["suggested_path"], "/location"
        )
        self.assertEqual(
            by_loc[("svc-geo", "/geo/lat")]["suggested_path"], "/location/lat"
        )
        self.assertEqual(by_loc[("svc-geo", "/geo/lat")]["reason"], "path_renamed")
        # 重命名子树中候选已删除的深字段：映射无效
        gone = by_loc[("svc-geo", "/geo/lng")]
        self.assertEqual(gone["status"], "broken")
        self.assertEqual(gone["reason"], "target_deleted")
        # 候选删除且无映射
        self.assertEqual(by_loc[("svc-code", "/code")]["reason"], "target_deleted")

    def test_baseline_target_missing(self):
        plan = self.c.plan_reference_migration("Forward", "1.0", "2.0")
        (ref,) = plan["references"]
        self.assertEqual(ref["source"]["type"], "schema")
        self.assertEqual(ref["source"]["schema"], "Legacy")
        self.assertEqual(ref["target_path"], "/later")
        self.assertEqual(ref["status"], "broken")
        self.assertEqual(ref["reason"], "baseline_target_missing")
        self.assertEqual(
            plan["summary"],
            {"total": 1, "source_schemas": 1, "source_assets": 0,
             "ready": 0, "renamed": 0, "broken": 1},
        )

    def test_no_references(self):
        self.c.register_schema("Empty", "1.0", obj(a={"type": "string"}))
        self.c.register_schema("Empty", "2.0", obj(a={"type": "string"}))
        plan = self.c.plan_reference_migration("Empty", "1.0", "2.0")
        self.assertEqual(plan["references"], [])
        self.assertEqual(
            plan["summary"],
            {"total": 0, "source_schemas": 0, "source_assets": 0,
             "ready": 0, "renamed": 0, "broken": 0},
        )

    def test_sorted_by_source_and_not_merged(self):
        plan = self.plan()
        keys = []
        for r in plan["references"]:
            src = r["source"]
            if src["type"] == "schema":
                keys.append((0, src["schema"], src["version"], src["path"]))
            else:
                keys.append((1, src["asset_id"]))
        self.assertEqual(keys, sorted(keys))
        # 同一目标路径来自不同来源时不合并：/zip 有 Order 与 svc-zip 两条
        zip_refs = [r for r in plan["references"] if r["target_path"] == "/zip"]
        self.assertEqual(len(zip_refs), 2)
        self.assertEqual(
            {r["source"]["type"] for r in zip_refs}, {"schema", "asset"}
        )

    def test_default_renames_empty(self):
        plan = self.c.plan_reference_migration("Address", "1.0", "2.0")
        self.assertEqual(plan["renames"], [])
        by_target = {r["target_path"]: r for r in plan["references"]
                     if r["source"].get("asset_id") == "svc-zip"}
        self.assertEqual(by_target["/zip"]["status"], "broken")
        self.assertEqual(by_target["/zip"]["reason"], "target_deleted")

    def test_renames_accept_pair_form(self):
        plan = self.c.plan_reference_migration(
            "Address", "1.0", "2.0", renames=[["/zip", "/zipcode"]]
        )
        self.assertEqual(plan["renames"], [{"from": "/zip", "to": "/zipcode"}])

    # --------------------------------------------------------------- 错误
    def test_non_string_identifiers(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.plan_reference_migration(123, "1.0", "2.0")
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.plan_reference_migration("Address", 1.0, "2.0")
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.plan_reference_migration("Address", "1.0", None)

    def test_same_version(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.plan_reference_migration("Address", "1.0", "1.0")
        self.assertEqual(cm.exception.details["reason"], "same_version")

    def test_renames_not_list(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.plan_reference_migration(
                "Address", "1.0", "2.0", renames={"from": "/zip"}
            )
        self.assertEqual(cm.exception.details["reason"], "renames_not_list")

    def test_rename_missing_keys(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.plan_reference_migration(
                "Address", "1.0", "2.0", renames=[{"from": "/zip"}]
            )
        self.assertEqual(cm.exception.details["reason"], "rename_invalid")

    def test_rename_invalid_pointer(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.plan_reference_migration(
                "Address", "1.0", "2.0",
                renames=[{"from": "zip", "to": "/zipcode"}],
            )
        self.assertEqual(cm.exception.details["reason"], "invalid_pointer")

    def test_rename_duplicate_from(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.plan_reference_migration(
                "Address", "1.0", "2.0",
                renames=[{"from": "/zip", "to": "/zipcode"},
                         {"from": "/zip", "to": "/street"}],
            )
        self.assertEqual(cm.exception.details["reason"], "rename_duplicate")

    def test_rename_colliding_to(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.plan_reference_migration(
                "Address", "1.0", "2.0",
                renames=[{"from": "/zip", "to": "/zipcode"},
                         {"from": "/code", "to": "/zipcode"}],
            )
        self.assertEqual(cm.exception.details["reason"], "rename_duplicate")

    def test_rename_endpoints_not_in_fields(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.plan_reference_migration(
                "Address", "1.0", "2.0",
                renames=[{"from": "/missing", "to": "/zipcode"}],
            )
        self.assertEqual(cm.exception.details["reason"], "rename_from_not_found")
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.plan_reference_migration(
                "Address", "1.0", "2.0",
                renames=[{"from": "/zip", "to": "/nowhere"}],
            )
        self.assertEqual(cm.exception.details["reason"], "rename_to_not_found")

    def test_rename_ambiguous_ancestors(self):
        # /geo 与 /geo/lat 同时作为重命名根：引用 /geo/lat 落入两个祖先。
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.plan_reference_migration(
                "Address", "1.0", "2.0",
                renames=[{"from": "/geo", "to": "/location"},
                         {"from": "/geo/lat", "to": "/zipcode"}],
            )
        self.assertEqual(cm.exception.details["reason"], "rename_ambiguous")
        self.assertEqual(cm.exception.details["path"], "/geo/lat")

    def test_nested_rename_roots_ok_without_ambiguous_reference(self):
        c = MetaCatalog()
        c.register_schema("A", "1.0", obj(
            geo=obj(lat={"type": "number"}, lng={"type": "number"}),
        ))
        c.register_schema("A", "2.0", obj(
            location=obj(lng={"type": "number"}),
            latitude={"type": "number"},
        ))
        c.register_asset("svc", "服务", "service",
                         [{"schema": "A", "version": "1.0", "path": "/geo/lng"}])
        # 引用只落入 /geo 一个祖先，嵌套根 /geo/lat 不造成歧义。
        plan = c.plan_reference_migration(
            "A", "1.0", "2.0",
            renames=[{"from": "/geo", "to": "/location"},
                     {"from": "/geo/lat", "to": "/latitude"}],
        )
        (ref,) = plan["references"]
        self.assertEqual(ref["status"], "renamed")
        self.assertEqual(ref["suggested_path"], "/location/lng")

    def test_not_found(self):
        with self.assertRaises(NotFoundError):
            self.c.plan_reference_migration("Missing", "1.0", "2.0")
        with self.assertRaises(NotFoundError):
            self.c.plan_reference_migration("Address", "9.9", "2.0")
        with self.assertRaises(NotFoundError):
            self.c.plan_reference_migration("Address", "1.0", "9.9")

    def test_too_many_edges(self):
        old = limits.MAX_IMPACT_VISITED
        limits.MAX_IMPACT_VISITED = 1
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                self.plan()
            self.assertEqual(cm.exception.details["reason"], "edges_exceeded")
        finally:
            limits.MAX_IMPACT_VISITED = old

    def test_too_many_assets(self):
        old = limits.MAX_IMPACT_ASSETS
        limits.MAX_IMPACT_ASSETS = 1
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                self.plan()
            self.assertEqual(cm.exception.details["reason"], "assets_exceeded")
        finally:
            limits.MAX_IMPACT_ASSETS = old

    # ----------------------------------------------------------- 只读语义
    def test_does_not_mutate_registry_reports_or_index(self):
        versions_before = self.c.list_versions("Address")
        search_before = self.c.search("street")
        n_reports = len(self.c.list_reports())

        self.plan()

        self.assertEqual(self.c.list_versions("Address"), versions_before)
        self.assertEqual(len(self.c.list_reports()), n_reports)
        self.assertEqual(self.c.search("street"), search_before)

    def test_deterministic_across_calls(self):
        self.assertEqual(self.plan(), self.plan())

    def test_result_is_independent_copy(self):
        plan = self.plan()
        plan["references"].clear()
        plan["summary"]["total"] = -1
        again = self.plan()
        self.assertEqual(again["summary"]["total"], 10)
        self.assertEqual(len(again["references"]), 10)


if __name__ == "__main__":
    unittest.main()
