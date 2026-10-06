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


ADDRESS_V1 = obj(
    street={"type": "string"},
    zip={"type": "string"},
    geo=obj(lat={"type": "number"}, lng={"type": "number"}),
    code={"type": "string"},
)

ADDRESS_V2 = obj(
    street={"type": "string"},
    postal={"type": "string"},
    location=obj(lat={"type": "number"}, lng={"type": "number"}),
)

RENAMES = [{"from": "/zip", "to": "/postal"}, {"from": "/geo", "to": "/location"}]


class ReferenceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        # 前向引用：目标字段 /ghost 在基线版本中不存在。
        self.c.register_schema(
            "Legacy", "1.0",
            obj(old={"$ref": "Address@1.0#/properties/ghost"}),
        )
        self.c.register_schema("Address", "1.0", ADDRESS_V1)
        self.c.register_schema("Address", "2.0", ADDRESS_V2)
        self.c.register_schema(
            "Person", "1.0",
            obj(
                name={"type": "string"},
                home={"$ref": "Address@1.0#"},
                work={"$ref": "Address@1.0#/properties/street"},
            ),
        )
        self.c.register_schema(
            "Person", "2.0",
            obj(addr={"$ref": "Address@1.0#/properties/geo"}),
        )
        self.c.register_schema(
            "Order", "1.0",
            obj(ship={"$ref": "Address@1.0#/properties/geo/properties/lat"}),
        )
        self.c.register_asset(
            "svc-street", "街道服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/street"}],
        )
        self.c.register_asset(
            "svc-zip", "邮编服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/zip"}],
        )
        self.c.register_asset(
            "svc-geo-deep", "坐标服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/geo/lat"}],
        )
        self.c.register_asset(
            "svc-code", "编码服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/code"}],
        )
        self.c.register_asset(
            "svc-other", "无关服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/name"}],
        )

    def plan(self, **kwargs):
        args = dict(renames=RENAMES)
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
             {"from": "/zip", "to": "/postal"}],
        )
        json.dumps(plan, ensure_ascii=False)  # 可 JSON 序列化

    def test_reference_entry_shape(self):
        plan = self.plan()
        for entry in plan["references"]:
            self.assertEqual(
                list(entry.keys()),
                ["source_type", "source_schema", "source_version",
                 "source_path", "asset_id", "target_schema", "target_path",
                 "suggested_version", "suggested_path", "status", "reason"],
            )
            self.assertEqual(entry["target_schema"], "Address")

    # ------------------------------------------------------------- 分类
    def test_status_classification(self):
        plan = self.plan()
        by_locator = {}
        for e in plan["references"]:
            key = (e["source_type"], e["source_schema"] or e["asset_id"],
                   e["source_path"] or "", e["target_path"])
            by_locator[key] = e

        # 跨 Schema $ref：根引用与未变字段 -> ready
        e = by_locator[("schema", "Person", "/home", "")]
        self.assertEqual(
            (e["status"], e["reason"], e["suggested_version"], e["suggested_path"]),
            ("ready", "path_unchanged", "2.0", ""),
        )
        e = by_locator[("schema", "Person", "/work", "/street")]
        self.assertEqual((e["status"], e["reason"], e["suggested_path"]),
                         ("ready", "path_unchanged", "/street"))

        # 显式映射根与重命名子树深层引用（保留后缀）-> renamed
        e = by_locator[("schema", "Person", "/addr", "/geo")]
        self.assertEqual((e["status"], e["reason"], e["suggested_path"]),
                         ("renamed", "path_renamed", "/location"))
        e = by_locator[("schema", "Order", "/ship", "/geo/lat")]
        self.assertEqual((e["status"], e["reason"], e["suggested_path"]),
                         ("renamed", "path_renamed", "/location/lat"))

        # 引用未解析基线字段 -> broken / baseline_target_missing
        e = by_locator[("schema", "Legacy", "/old", "/ghost")]
        self.assertEqual(
            (e["status"], e["reason"], e["suggested_version"], e["suggested_path"]),
            ("broken", "baseline_target_missing", None, None),
        )

        # 资产直接引用
        e = by_locator[("asset", "svc-street", "", "/street")]
        self.assertEqual((e["status"], e["reason"], e["suggested_path"]),
                         ("ready", "path_unchanged", "/street"))
        e = by_locator[("asset", "svc-zip", "", "/zip")]
        self.assertEqual((e["status"], e["reason"], e["suggested_path"]),
                         ("renamed", "path_renamed", "/postal"))
        e = by_locator[("asset", "svc-geo-deep", "", "/geo/lat")]
        self.assertEqual((e["status"], e["reason"], e["suggested_path"]),
                         ("renamed", "path_renamed", "/location/lat"))

        # 基线存在、候选删除且无映射 -> broken / target_deleted
        e = by_locator[("asset", "svc-code", "", "/code")]
        self.assertEqual(
            (e["status"], e["reason"], e["suggested_version"], e["suggested_path"]),
            ("broken", "target_deleted", None, None),
        )

    def test_summary_counts(self):
        plan = self.plan()
        self.assertEqual(
            plan["summary"],
            {"total": 9, "source_schemas": 4, "source_assets": 4,
             "ready": 3, "renamed": 4, "broken": 2},
        )

    def test_sources_not_merged_and_stable_order(self):
        plan = self.plan()
        kinds = [
            (e["source_type"], e["source_schema"] or e["asset_id"], e["target_path"])
            for e in plan["references"]
        ]
        self.assertEqual(
            kinds,
            [
                ("schema", "Legacy", "/ghost"),
                ("schema", "Order", "/geo/lat"),
                ("schema", "Person", ""),
                ("schema", "Person", "/street"),
                ("schema", "Person", "/geo"),
                ("asset", "svc-code", "/code"),
                ("asset", "svc-geo-deep", "/geo/lat"),
                ("asset", "svc-street", "/street"),
                ("asset", "svc-zip", "/zip"),
            ],
        )
        # 同一目标字段的 Schema 引用与资产引用各自保留，不合并。
        street = [e for e in plan["references"] if e["target_path"] == "/street"]
        self.assertEqual(len(street), 2)
        self.assertEqual({e["source_type"] for e in street}, {"schema", "asset"})

    def test_renames_as_pairs_and_empty(self):
        plan = self.plan(renames=[["/zip", "/postal"], ["/geo", "/location"]])
        self.assertEqual(plan["summary"]["renamed"], 4)
        # 不提供映射时，被删除字段的引用为 broken / target_deleted。
        plan = self.c.plan_reference_migration("Address", "1.0", "2.0")
        self.assertEqual(plan["renames"], [])
        svc_zip = next(
            e for e in plan["references"] if e["asset_id"] == "svc-zip"
        )
        self.assertEqual((svc_zip["status"], svc_zip["reason"]),
                         ("broken", "target_deleted"))

    # ------------------------------------------------------------- 只读性
    def test_read_only_and_deterministic(self):
        before_reports = self.c.list_reports()
        before_versions = self.c.list_versions("Address")
        before_search = self.c.search(schema="Address")
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(self.c.list_reports(), before_reports)
        self.assertEqual(self.c.list_versions("Address"), before_versions)
        self.assertEqual(self.c.search(schema="Address"), before_search)

    # ------------------------------------------------------------- 参数错误
    def test_invalid_arguments(self):
        bad_calls = [
            lambda: self.c.plan_reference_migration(123, "1.0", "2.0"),
            lambda: self.c.plan_reference_migration("Address", 1.0, "2.0"),
            lambda: self.c.plan_reference_migration("Address", "1.0", None),
            lambda: self.c.plan_reference_migration("Address", "1.0", "1.0"),
            lambda: self.plan(renames="not-a-list"),
            lambda: self.plan(renames=[{"from": "/zip"}]),
            lambda: self.plan(renames=[{"from": "zip", "to": "/postal"}]),
            lambda: self.plan(renames=[{"from": "/nope", "to": "/postal"}]),
            lambda: self.plan(renames=[{"from": "/zip", "to": "/nope"}]),
            lambda: self.plan(renames=[
                {"from": "/zip", "to": "/postal"},
                {"from": "/zip", "to": "/location"},
            ]),
            lambda: self.plan(renames=[
                {"from": "/zip", "to": "/postal"},
                {"from": "/code", "to": "/postal"},
            ]),
        ]
        for call in bad_calls:
            with self.assertRaises(SchemaComparisonInvalid):
                call()

    def test_not_found(self):
        with self.assertRaises(NotFoundError):
            self.c.plan_reference_migration("Nope", "1.0", "2.0")
        with self.assertRaises(NotFoundError):
            self.c.plan_reference_migration("Address", "1.0", "9.9")
        with self.assertRaises(NotFoundError):
            self.c.plan_reference_migration("Address", "9.9", "2.0")

    # ------------------------------------------------------------- 限制
    def test_edges_limit(self):
        old = limits.MAX_IMPACT_VISITED
        limits.MAX_IMPACT_VISITED = 1
        try:
            with self.assertRaises(ImpactAnalysisTooLarge):
                self.plan()
        finally:
            limits.MAX_IMPACT_VISITED = old

    def test_assets_limit(self):
        old = limits.MAX_IMPACT_ASSETS
        limits.MAX_IMPACT_ASSETS = 3
        try:
            with self.assertRaises(ImpactAnalysisTooLarge):
                self.plan()
        finally:
            limits.MAX_IMPACT_ASSETS = old


if __name__ == "__main__":
    unittest.main()
