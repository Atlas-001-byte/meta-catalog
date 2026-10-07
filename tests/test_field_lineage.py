import json
import unittest

from meta_catalog import MetaCatalog
from meta_catalog.errors import (
    FieldTraceAmbiguous,
    FieldTraceInvalid,
    NotFoundError,
)


def obj(**props_and_req):
    req = props_and_req.pop("__required__", [])
    doc = {"type": "object", "properties": props_and_req}
    if req:
        doc["required"] = req
    return doc


V1 = obj(
    name={"type": "string"},
    age={"type": "integer"},
    geo=obj(lat={"type": "number"}, lng={"type": "number"}),
    code={"type": "string"},
)
V2 = obj(
    name={"type": "string"},
    years={"type": "integer"},
    location=obj(lat={"type": "number"}, lng={"type": "number"}),
)
V3 = obj(
    name={"type": "string"},
    years={"type": "number"},
    location=obj(lat={"type": "number"}),
)
RENAMES_12 = [
    {"from": "/age", "to": "/years"},
    {"from": "/geo", "to": "/location"},
]


class FieldLineageTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema("S", "1.0", V1)
        self.c.register_schema("S", "2.0", V2)
        self.c.register_schema("S", "3.0", V3)
        self.r12 = self.c.compare_schemas(
            "S", "1.0", candidate_version="2.0", renames=RENAMES_12
        )
        self.r23 = self.c.compare_schemas("S", "2.0", candidate_version="3.0")

    # ------------------------------------------------------------- 结构
    def test_top_level_shape(self):
        r = self.c.trace_field_lineage("S", "1.0", "3.0", "/age")
        self.assertEqual(
            list(r.keys()),
            ["schema", "baseline_version", "target_version", "path",
             "target_path", "summary", "steps"],
        )
        self.assertEqual(r["schema"], "S")
        self.assertEqual(r["baseline_version"], "1.0")
        self.assertEqual(r["target_version"], "3.0")
        self.assertEqual(r["path"], "/age")
        self.assertEqual(r["target_path"], "/years")
        self.assertEqual(r["summary"]["status"], "renamed")
        json.dumps(r, ensure_ascii=False)

    def test_step_shape(self):
        r = self.c.trace_field_lineage("S", "1.0", "3.0", "/age")
        self.assertEqual(r["summary"]["steps"], 2)
        self.assertEqual(
            list(r["steps"][0].keys()),
            ["report_id", "from_version", "to_version", "old_path",
             "new_path", "change_kind", "compatibility"],
        )
        self.assertEqual(r["steps"][0]["report_id"], self.r12["report_id"])
        self.assertEqual(r["steps"][1]["report_id"], self.r23["report_id"])
        self.assertEqual(
            [(s["from_version"], s["to_version"]) for s in r["steps"]],
            [("1.0", "2.0"), ("2.0", "3.0")],
        )

    def test_same_start_end_is_empty(self):
        r = self.c.trace_field_lineage("S", "2.0", "2.0", "/name")
        self.assertEqual(r["steps"], [])
        self.assertEqual(r["target_path"], "/name")
        self.assertEqual(r["path"], "/name")
        self.assertEqual(r["summary"], {"status": "unchanged", "steps": 0, "changes": 0})

    # ------------------------------------------------------------- 血缘
    def test_explicit_rename_then_modified(self):
        r = self.c.trace_field_lineage("S", "1.0", "3.0", "/age")
        self.assertEqual(r["target_path"], "/years")
        self.assertEqual(r["summary"]["status"], "renamed")
        kinds = [(s["change_kind"], s["compatibility"]) for s in r["steps"]]
        self.assertEqual(kinds, [("rename", "breaking"), ("modified", "compatible")])
        # old_path / new_path 逐步衔接。
        self.assertEqual(
            [(s["old_path"], s["new_path"]) for s in r["steps"]],
            [("/age", "/years"), ("/years", "/years")],
        )

    def test_rename_subtree_deep_keeps_suffix(self):
        r = self.c.trace_field_lineage("S", "1.0", "2.0", "/geo/lat")
        self.assertEqual(r["target_path"], "/location/lat")
        step = r["steps"][0]
        self.assertEqual(step["change_kind"], "rename")
        self.assertEqual(step["compatibility"], "breaking")
        self.assertEqual(step["report_id"], self.r12["report_id"])
        self.assertEqual(step["old_path"], "/geo/lat")
        self.assertEqual(step["new_path"], "/location/lat")

    def test_deep_field_then_deleted_terminates(self):
        # /geo/lng 在 1->2 随重命名迁移，2->3 被删除。
        r = self.c.trace_field_lineage("S", "1.0", "3.0", "/geo/lng")
        self.assertIsNone(r["target_path"])
        self.assertEqual(r["summary"]["status"], "terminated")
        self.assertEqual(
            [(s["old_path"], s["new_path"], s["change_kind"]) for s in r["steps"]],
            [("/geo/lng", "/location/lng", "rename"),
             ("/location/lng", None, "deleted")],
        )

    def test_field_deleted_at_target_terminates(self):
        # /code 在 1.0 存在、2.0 删除且无重命名：目标版本即 2.0。
        r = self.c.trace_field_lineage("S", "1.0", "2.0", "/code")
        self.assertIsNone(r["target_path"])
        self.assertEqual(r["summary"]["status"], "terminated")
        self.assertEqual(r["steps"][0]["change_kind"], "deleted")
        self.assertEqual(r["steps"][0]["compatibility"], "breaking")
        self.assertEqual(r["steps"][0]["old_path"], "/code")
        self.assertIsNone(r["steps"][0]["new_path"])

    def test_unchanged_across_reports(self):
        r = self.c.trace_field_lineage("S", "1.0", "3.0", "/name")
        self.assertEqual(r["target_path"], "/name")
        self.assertEqual(r["summary"]["status"], "unchanged")
        self.assertEqual(r["summary"]["changes"], 0)
        for s in r["steps"]:
            self.assertEqual(s["change_kind"], "unchanged")
            self.assertEqual(s["compatibility"], "compatible")
            self.assertEqual(s["old_path"], s["new_path"])

    def test_intermediate_registered_target(self):
        r = self.c.trace_field_lineage("S", "1.0", "2.0", "/age")
        self.assertEqual(r["target_version"], "2.0")
        self.assertEqual(len(r["steps"]), 1)

    def test_inline_candidate_target_version(self):
        # 目标版本只来自内联候选比较，不注册。
        c = MetaCatalog()
        c.register_schema("S", "1.0", obj(a={"type": "string"}, b={"type": "string"}))
        c.compare_schemas(
            "S", "1.0",
            obj(a={"type": "string"}, zed={"type": "string"}),
            candidate_version="9.0",
            renames=[{"from": "/b", "to": "/zed"}],
        )
        r = c.trace_field_lineage("S", "1.0", "9.0", "/b")
        self.assertEqual(r["target_path"], "/zed")
        self.assertEqual(r["steps"][0]["to_version"], "9.0")
        self.assertNotIn("9.0", c.list_versions("S"))
        # 未变化字段在仅存在于内联候选的目标版本上同路径保留。
        r2 = c.trace_field_lineage("S", "1.0", "9.0", "/a")
        self.assertEqual(r2["target_path"], "/a")
        self.assertEqual(r2["steps"][0]["change_kind"], "unchanged")

    # ------------------------------------------------------------- 唯一性 / 冲突
    def _two_conflicting_reports(self):
        c = MetaCatalog()
        c.register_schema("S", "1.0", obj(a={"type": "string"}, keep={"type": "string"}))
        c.compare_schemas(
            "S", "1.0", obj(x={"type": "string"}, keep={"type": "string"}),
            candidate_version="2.0", renames=[{"from": "/a", "to": "/x"}],
        )
        c.compare_schemas(
            "S", "1.0", obj(y={"type": "string"}, keep={"type": "string"}),
            candidate_version="2.0", renames=[{"from": "/a", "to": "/y"}],
        )
        return c

    def test_conflicting_rename_targets_ambiguous(self):
        c = self._two_conflicting_reports()
        with self.assertRaises(FieldTraceAmbiguous) as ctx:
            c.trace_field_lineage("S", "1.0", "2.0", "/a")
        details = ctx.exception.details
        self.assertEqual(details["reason"], "conflicting_reports")
        conflicts = details["conflicts"]
        self.assertEqual({c2["new_path"] for c2 in conflicts}, {"/x", "/y"})
        for c2 in conflicts:
            self.assertEqual(c2["from_version"], "1.0")
            self.assertEqual(c2["to_version"], "2.0")
            self.assertTrue(c2["report_id"])

    def test_same_conclusion_reports_merge(self):
        c = self._two_conflicting_reports()
        r = c.trace_field_lineage("S", "1.0", "2.0", "/keep")
        self.assertEqual(len(r["steps"]), 1)
        self.assertEqual(r["steps"][0]["change_kind"], "unchanged")

    def test_conflict_beyond_target_not_ambiguous(self):
        # 2.0 与 3.0 对 /a 冲突，但只追溯到 2.0：唯一。
        c = MetaCatalog()
        c.register_schema("S", "1.0", obj(a={"type": "integer"}))
        c.register_schema("S", "2.0", obj(a={"type": "integer"}))
        c.compare_schemas("S", "1.0", candidate_version="2.0")
        c.compare_schemas(
            "S", "2.0", obj(p={"type": "integer"}), candidate_version="3.0",
            renames=[{"from": "/a", "to": "/p"}],
        )
        c.compare_schemas(
            "S", "2.0", obj(q={"type": "integer"}), candidate_version="3.0",
            renames=[{"from": "/a", "to": "/q"}],
        )
        r = c.trace_field_lineage("S", "1.0", "2.0", "/a")
        self.assertEqual(r["target_path"], "/a")
        with self.assertRaises(FieldTraceAmbiguous):
            c.trace_field_lineage("S", "1.0", "3.0", "/a")

    def test_dead_branch_pruned(self):
        # 一条分支在到达目标前终止，另一条可到达：唯一，不判歧义。
        c = MetaCatalog()
        c.register_schema("S", "1.0", obj(a={"type": "integer"}))
        c.register_schema("S", "2.0", obj(a={"type": "integer"}))
        c.compare_schemas("S", "1.0", candidate_version="2.0")
        c.compare_schemas(
            "S", "2.0", obj(p={"type": "integer"}), candidate_version="3.0",
            renames=[{"from": "/a", "to": "/p"}],
        )
        c.compare_schemas(
            "S", "2.0", obj(gone={"type": "integer"}), candidate_version="2.5"
        )
        r = c.trace_field_lineage("S", "1.0", "3.0", "/a")
        self.assertEqual(r["target_path"], "/p")
        self.assertEqual(len(r["steps"]), 2)

    def test_cycles_are_invalid_but_shortest_path_found(self):
        c = MetaCatalog()
        c.register_schema("S", "1.0", obj(a={"type": "string"}))
        c.register_schema("S", "2.0", obj(a={"type": "string"}, n={"type": "string"}))
        c.compare_schemas("S", "1.0", candidate_version="2.0")
        c.compare_schemas("S", "2.0", candidate_version="1.0")
        r = c.trace_field_lineage("S", "1.0", "2.0", "/a")
        self.assertEqual(len(r["steps"]), 1)
        self.assertEqual(r["target_path"], "/a")

    def test_report_id_used_at_most_once_per_route(self):
        # 同一报告构成 1.0<->2.0 的两边之一时，路线不得重复使用该 report_id。
        c = MetaCatalog()
        c.register_schema("S", "1.0", obj(a={"type": "string"}))
        c.register_schema("S", "2.0", obj(a={"type": "string"}))
        c.compare_schemas("S", "1.0", candidate_version="2.0")
        c.compare_schemas("S", "2.0", candidate_version="1.0")
        r = c.trace_field_lineage("S", "1.0", "2.0", "/a")
        ids = [s["report_id"] for s in r["steps"]]
        self.assertEqual(len(ids), len(set(ids)))

    # ------------------------------------------------------------- 错误
    def test_invalid_arguments(self):
        bad = [
            lambda: self.c.trace_field_lineage(123, "1.0", "2.0", "/name"),
            lambda: self.c.trace_field_lineage("", "1.0", "2.0", "/name"),
            lambda: self.c.trace_field_lineage("S", 1, "2.0", "/name"),
            lambda: self.c.trace_field_lineage("S", "1.0", None, "/name"),
            lambda: self.c.trace_field_lineage("S", "1.0", "2.0", None),
            lambda: self.c.trace_field_lineage("S", "1.0", "2.0", 42),
            lambda: self.c.trace_field_lineage("S", "1.0", "2.0", "name"),
            lambda: self.c.trace_field_lineage("S", "1.0", "2.0", "/properties/name"),
        ]
        for call in bad:
            with self.assertRaises(FieldTraceInvalid):
                call()

    def test_not_found(self):
        with self.assertRaises(NotFoundError):
            self.c.trace_field_lineage("Nope", "1.0", "2.0", "/name")
        with self.assertRaises(NotFoundError):
            self.c.trace_field_lineage("S", "9.0", "2.0", "/name")
        with self.assertRaises(NotFoundError):
            self.c.trace_field_lineage("S", "1.0", "2.0", "/nope")
        # 无任何报告可到达目标版本。
        with self.assertRaises(NotFoundError):
            self.c.trace_field_lineage("S", "1.0", "8.0", "/name")
        # 字段在到达目标版本前终止。
        with self.assertRaises(NotFoundError):
            self.c.trace_field_lineage("S", "1.0", "3.0", "/code")

    def test_logical_pointer_with_literal_properties_property(self):
        # 属性恰好名为 properties：/properties/x 是合法逻辑路径。
        c = MetaCatalog()
        c.register_schema(
            "S", "1.0",
            obj(**{"properties": obj(x={"type": "string"})}),
        )
        c.register_schema(
            "S", "2.0",
            obj(**{"properties": obj(x={"type": "string"})}),
        )
        c.compare_schemas("S", "1.0", candidate_version="2.0")
        r = c.trace_field_lineage("S", "1.0", "2.0", "/properties/x")
        self.assertEqual(r["target_path"], "/properties/x")

    # ------------------------------------------------------------- 只读 / 确定
    def test_read_only_deterministic_and_independent(self):
        before_reports = self.c.list_reports()
        before_versions = self.c.list_versions("S")
        before_search = self.c.search(schema="S")
        first = self.c.trace_field_lineage("S", "1.0", "3.0", "/geo/lat")
        second = self.c.trace_field_lineage("S", "1.0", "3.0", "/geo/lat")
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )
        # 返回值为独立副本：篡改不影响再次读取。
        first["steps"][0]["new_path"] = "/tampered"
        third = self.c.trace_field_lineage("S", "1.0", "3.0", "/geo/lat")
        self.assertEqual(third["steps"][0]["new_path"], "/location/lat")
        self.assertEqual(self.c.list_reports(), before_reports)
        self.assertEqual(self.c.list_versions("S"), before_versions)
        self.assertEqual(self.c.search(schema="S"), before_search)

    def test_other_entrypoints_unaffected(self):
        # 血缘调用不应改变既有报告内容。
        before = self.c.get_report(self.r12["report_id"])
        self.c.trace_field_lineage("S", "1.0", "3.0", "/age")
        self.c.trace_field_lineage("S", "1.0", "3.0", "/geo/lat")
        self.assertEqual(self.c.get_report(self.r12["report_id"]), before)

    def test_root_path_supported(self):
        c = MetaCatalog()
        c.register_schema("S", "1.0", obj(a={"type": "string"}))
        c.register_schema("S", "2.0", obj(a={"type": "string"}, b={"type": "string"}))
        c.compare_schemas("S", "1.0", candidate_version="2.0")
        r = c.trace_field_lineage("S", "1.0", "2.0", "")
        self.assertEqual(r["path"], "")
        self.assertEqual(r["target_path"], "")
        self.assertEqual(r["steps"][0]["change_kind"], "unchanged")


if __name__ == "__main__":
    unittest.main()
