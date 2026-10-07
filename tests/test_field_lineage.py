import copy
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


def build_chain_catalog():
    """Person 1.0 -> 2.0（age 改名 years）-> 3.0（新增 nick）。"""
    c = MetaCatalog()
    c.register_schema(
        "Person", "1.0",
        obj(name={"type": "string"}, age={"type": "integer"}),
    )
    c.register_schema(
        "Person", "2.0",
        obj(name={"type": "string"}, years={"type": "integer"}),
    )
    c.register_schema(
        "Person", "3.0",
        obj(
            name={"type": "string"},
            years={"type": "integer"},
            nick={"type": "string"},
        ),
    )
    r12 = c.compare_schemas(
        "Person", "1.0", None, candidate_version="2.0",
        renames=[{"from": "/age", "to": "/years"}],
    )
    r23 = c.compare_schemas("Person", "2.0", None, candidate_version="3.0")
    return c, r12["report_id"], r23["report_id"]


class FieldLineageBasicTests(unittest.TestCase):
    def setUp(self):
        self.c, self.rid12, self.rid23 = build_chain_catalog()

    def test_rename_then_unchanged(self):
        result = self.c.trace_field_lineage("Person", "1.0", "3.0", "/age")
        self.assertEqual(
            result["schema"], "Person",
        )
        self.assertEqual(result["baseline_version"], "1.0")
        self.assertEqual(result["target_version"], "3.0")
        self.assertEqual(result["path"], "/age")
        self.assertEqual(result["target_path"], "/years")
        self.assertEqual(result["summary"]["steps"], 2)
        kinds = [s["change_kind"] for s in result["steps"]]
        self.assertEqual(kinds, ["rename", "unchanged"])
        self.assertEqual(
            result["steps"][0],
            {
                "report_id": self.rid12,
                "from_version": "1.0",
                "to_version": "2.0",
                "old_path": "/age",
                "new_path": "/years",
                "change_kind": "rename",
                "compatibility": "breaking",
            },
        )
        self.assertEqual(
            result["steps"][1],
            {
                "report_id": self.rid23,
                "from_version": "2.0",
                "to_version": "3.0",
                "old_path": "/years",
                "new_path": "/years",
                "change_kind": "unchanged",
                "compatibility": "compatible",
            },
        )
        self.assertEqual(
            result["summary"]["change_kinds"], {"rename": 1, "unchanged": 1}
        )
        # 顶层键完整且可 JSON 序列化。
        self.assertEqual(
            set(result),
            {
                "schema", "baseline_version", "target_version", "path",
                "target_path", "summary", "steps",
            },
        )
        json.dumps(result)

    def test_retained_field_all_unchanged(self):
        result = self.c.trace_field_lineage("Person", "1.0", "3.0", "/name")
        self.assertEqual(result["target_path"], "/name")
        self.assertEqual([s["change_kind"] for s in result["steps"]],
                         ["unchanged", "unchanged"])
        for step in result["steps"]:
            self.assertEqual(step["old_path"], "/name")
            self.assertEqual(step["new_path"], "/name")
            self.assertEqual(step["compatibility"], "compatible")

    def test_intermediate_target(self):
        result = self.c.trace_field_lineage("Person", "1.0", "2.0", "/age")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["target_path"], "/years")

    def test_same_start_and_end_empty_steps(self):
        result = self.c.trace_field_lineage("Person", "2.0", "2.0", "/years")
        self.assertEqual(result["steps"], [])
        self.assertEqual(result["target_path"], "/years")
        self.assertEqual(result["path"], "/years")
        self.assertEqual(result["summary"], {"steps": 0, "change_kinds": {}})

    def test_root_path_lineage(self):
        result = self.c.trace_field_lineage("Person", "1.0", "3.0", "")
        self.assertEqual(result["target_path"], "")
        self.assertEqual(len(result["steps"]), 2)
        self.assertTrue(all(s["new_path"] == "" for s in result["steps"]))

    def test_modified_change_passes_report_through(self):
        c = MetaCatalog()
        c.register_schema(
            "S", "1.0",
            obj(tags={"type": "array", "items": {"type": "string"}}),
        )
        c.register_schema(
            "S", "2.0",
            obj(tags={"type": "array", "items": {"type": "integer"}}),
        )
        report = c.compare_schemas("S", "1.0", None, candidate_version="2.0")
        entry = next(e for e in report["changes"] if e["path"] == "/tags/-")
        result = c.trace_field_lineage("S", "1.0", "2.0", "/tags/-")
        self.assertEqual(len(result["steps"]), 1)
        step = result["steps"][0]
        self.assertEqual(step["old_path"], "/tags/-")
        self.assertEqual(step["new_path"], "/tags/-")
        self.assertEqual(step["change_kind"], entry["change_kind"])
        self.assertEqual(step["compatibility"], "breaking")
        self.assertEqual(step["report_id"], report["report_id"])

    def test_metadata_change_passes_report_through(self):
        c = MetaCatalog()
        c.register_schema(
            "S", "1.0",
            obj(name={"type": "string", "title": "旧"}),
        )
        c.register_schema(
            "S", "2.0",
            obj(name={"type": "string", "title": "新"}),
        )
        report = c.compare_schemas("S", "1.0", None, candidate_version="2.0")
        entry = next(e for e in report["changes"] if e["path"] == "/name")
        self.assertEqual(entry["change_kind"], "metadata")
        result = c.trace_field_lineage("S", "1.0", "2.0", "/name")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["change_kind"], "metadata")
        self.assertEqual(result["steps"][0]["compatibility"], "metadata")
        self.assertEqual(result["target_path"], "/name")


class RenameSubtreeTests(unittest.TestCase):
    def test_deep_field_keeps_suffix(self):
        c = MetaCatalog()
        c.register_schema(
            "Addr", "1.0",
            obj(street={"type": "string"},
                geo=obj(lat={"type": "number"}, lng={"type": "number"})),
        )
        c.register_schema(
            "Addr", "2.0",
            obj(street={"type": "string"},
                location=obj(lat={"type": "number"}, lng={"type": "number"})),
        )
        c.compare_schemas(
            "Addr", "1.0", None, candidate_version="2.0",
            renames=[{"from": "/geo", "to": "/location"}],
        )
        result = c.trace_field_lineage("Addr", "1.0", "2.0", "/geo/lat")
        self.assertEqual(len(result["steps"]), 1)
        step = result["steps"][0]
        self.assertEqual(step["old_path"], "/geo/lat")
        self.assertEqual(step["new_path"], "/location/lat")
        self.assertEqual(step["change_kind"], "rename")
        self.assertEqual(step["compatibility"], "breaking")

        # 深层结构同时发生变化时沿用报告条目的 kind/compatibility。
        c2 = MetaCatalog()
        c2.register_schema(
            "Addr", "1.0",
            obj(geo=obj(lat={"type": "integer"})),
        )
        c2.register_schema(
            "Addr", "2.0",
            obj(location=obj(lat={"type": "number"})),
        )
        c2.compare_schemas(
            "Addr", "1.0", None, candidate_version="2.0",
            renames=[{"from": "/geo", "to": "/location"}],
        )
        r2 = c2.trace_field_lineage("Addr", "1.0", "2.0", "/geo/lat")
        self.assertEqual(r2["steps"][0]["new_path"], "/location/lat")
        self.assertEqual(r2["steps"][0]["change_kind"], "modified")


class TerminationTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema("P", "1.0", obj(a={"type": "string"},
                                               b={"type": "string"}))
        self.c.register_schema("P", "2.0", obj(a={"type": "string"}))
        self.c.compare_schemas("P", "1.0", None, candidate_version="2.0")
        # 3.0 仅作为报告候选标签（未注册）。
        self.c.compare_schemas(
            "P", "2.0",
            obj(a={"type": "string"}, x={"type": "string"}),
            candidate_version="3.0",
        )

    def test_deleted_field_terminates_at_next_version(self):
        with self.assertRaises(NotFoundError) as cm:
            self.c.trace_field_lineage("P", "1.0", "2.0", "/b")
        self.assertEqual(cm.exception.code, "NotFound")
        self.assertEqual(cm.exception.details["path"], "/b")

    def test_no_route_past_termination_even_though_target_exists(self):
        with self.assertRaises(NotFoundError):
            self.c.trace_field_lineage("P", "1.0", "3.0", "/b")
        # 未终止的字段可以继续到达仅报告标签的 3.0。
        result = self.c.trace_field_lineage("P", "1.0", "3.0", "/a")
        self.assertEqual(result["target_path"], "/a")
        self.assertEqual(len(result["steps"]), 2)


class ReportCandidateLabelTests(unittest.TestCase):
    def test_target_can_be_unregistered_report_label(self):
        c = MetaCatalog()
        c.register_schema("P", "1.0", obj(age={"type": "integer"}))
        c.compare_schemas(
            "P", "1.0",
            obj(years={"type": "integer"}),
            candidate_version="9.0",
            renames=[{"from": "/age", "to": "/years"}],
        )
        result = c.trace_field_lineage("P", "1.0", "9.0", "/age")
        self.assertEqual(result["target_path"], "/years")
        self.assertEqual(result["steps"][0]["to_version"], "9.0")


class AmbiguityTests(unittest.TestCase):
    def test_conflicting_reports_between_same_versions(self):
        c = MetaCatalog()
        c.register_schema(
            "P", "1.0",
            obj(name={"type": "string"}, age={"type": "integer"}),
        )
        ra = c.compare_schemas(
            "P", "1.0",
            obj(name={"type": "string"}, years={"type": "integer"}),
            candidate_version="5.0",
            renames=[{"from": "/age", "to": "/years"}],
        )
        rb = c.compare_schemas(
            "P", "1.0",
            obj(name={"type": "string"}, age={"type": "string"}),
            candidate_version="5.0",
        )
        with self.assertRaises(FieldTraceAmbiguous) as cm:
            c.trace_field_lineage("P", "1.0", "5.0", "/age")
        self.assertEqual(cm.exception.code, "FieldTraceAmbiguous")
        details = cm.exception.details
        self.assertEqual(details["reason"], "conflicting_reports")
        self.assertEqual(details["from_version"], "1.0")
        self.assertEqual(details["to_version"], "5.0")
        self.assertEqual(details["path"], "/age")
        report_ids = {item["report_id"] for item in details["conflicts"]}
        self.assertEqual(report_ids, {ra["report_id"], rb["report_id"]})
        paths = {p for item in details["conflicts"] for p in item["paths"]}
        self.assertEqual(paths, {"/age", "/years"})

    def test_move_vs_delete_conflict(self):
        c = MetaCatalog()
        c.register_schema("P", "1.0", obj(age={"type": "integer"}))
        c.compare_schemas(
            "P", "1.0", obj(years={"type": "integer"}),
            candidate_version="5.0",
            renames=[{"from": "/age", "to": "/years"}],
        )
        c.compare_schemas(
            "P", "1.0", obj(other={"type": "string"}),
            candidate_version="5.0",
        )
        with self.assertRaises(FieldTraceAmbiguous):
            c.trace_field_lineage("P", "1.0", "5.0", "/age")

    def test_consistent_reports_share_outcome(self):
        c = MetaCatalog()
        c.register_schema(
            "P", "1.0",
            obj(age={"type": "integer"}, keep={"type": "string"}),
        )
        ra = c.compare_schemas(
            "P", "1.0",
            obj(years={"type": "integer"}, keep={"type": "string"},
                extra1={"type": "string"}),
            candidate_version="6.0",
            renames=[{"from": "/age", "to": "/years"}],
        )
        rb = c.compare_schemas(
            "P", "1.0",
            obj(years={"type": "integer"}, keep={"type": "string"},
                extra2={"type": "integer"}),
            candidate_version="6.0",
            renames=[{"from": "/age", "to": "/years"}],
        )
        result = c.trace_field_lineage("P", "1.0", "6.0", "/age")
        self.assertEqual(len(result["steps"]), 1)
        self.assertIn(result["steps"][0]["report_id"],
                      {ra["report_id"], rb["report_id"]})
        self.assertEqual(result["target_path"], "/years")


class RouteSelectionTests(unittest.TestCase):
    def test_shortest_route_preferred_in_diamond(self):
        c = MetaCatalog()
        c.register_schema("P", "1.0", obj(a={"type": "string"}))
        c.register_schema("P", "2.0", obj(b={"type": "string"}))
        c.register_schema(
            "P", "3.0",
            {"type": "object", "properties": {"c": {"type": "string"}}},
        )
        c.compare_schemas(
            "P", "1.0", None, candidate_version="2.0",
            renames=[{"from": "/a", "to": "/b"}],
        )
        c.compare_schemas(
            "P", "2.0", None, candidate_version="3.0",
            renames=[{"from": "/b", "to": "/c"}],
        )
        direct = c.compare_schemas(
            "P", "1.0", None, candidate_version="3.0",
            renames=[{"from": "/a", "to": "/c"}],
        )
        result = c.trace_field_lineage("P", "1.0", "3.0", "/a")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["report_id"], direct["report_id"])
        self.assertEqual(result["target_path"], "/c")

    def test_report_ring_does_not_loop(self):
        c = MetaCatalog()
        c.register_schema("P", "1.0", obj(a={"type": "string"}))
        c.register_schema("P", "2.0", obj(a={"type": "string"}))
        c.compare_schemas("P", "1.0", None, candidate_version="2.0")
        # 2.0 -> 1.0 的报告与上面构成报告环。
        c.compare_schemas(
            "P", "2.0",
            obj(a={"type": "string"}),
            candidate_version="1.0",
        )
        # 2.0 -> 3.0（仅标签）改名继续向外。
        c.compare_schemas(
            "P", "2.0",
            {"type": "object", "properties": {"c": {"type": "string"}}},
            candidate_version="3.0",
            renames=[{"from": "/a", "to": "/c"}],
        )
        result = c.trace_field_lineage("P", "1.0", "3.0", "/a")
        self.assertEqual(result["target_path"], "/c")
        report_ids = [s["report_id"] for s in result["steps"]]
        self.assertEqual(len(report_ids), len(set(report_ids)))
        self.assertEqual(len(result["steps"]), 2)


class InvalidRequestTests(unittest.TestCase):
    def setUp(self):
        self.c, _, _ = build_chain_catalog()

    def assertInvalid(self, **kwargs):
        with self.assertRaises(FieldTraceInvalid) as cm:
            self.c.trace_field_lineage(**kwargs)
        self.assertEqual(cm.exception.code, "FieldTraceInvalid")

    def test_name_must_be_non_empty_string(self):
        self.assertInvalid(name="", baseline_version="1.0",
                           target_version="2.0", path="/age")
        self.assertInvalid(name=123, baseline_version="1.0",
                           target_version="2.0", path="/age")

    def test_versions_must_be_non_empty_strings(self):
        self.assertInvalid(name="Person", baseline_version=1,
                           target_version="2.0", path="/age")
        self.assertInvalid(name="Person", baseline_version="1.0",
                           target_version=None, path="/age")

    def test_path_must_be_string_and_valid_pointer(self):
        self.assertInvalid(name="Person", baseline_version="1.0",
                           target_version="2.0", path=42)
        self.assertInvalid(name="Person", baseline_version="1.0",
                           target_version="2.0", path="age")
        self.assertInvalid(name="Person", baseline_version="1.0",
                           target_version="2.0", path="/age~2")

    def test_document_pointer_is_non_logical(self):
        self.assertInvalid(name="Person", baseline_version="1.0",
                           target_version="2.0", path="/properties/age")
        self.assertInvalid(name="Person", baseline_version="1.0",
                           target_version="2.0", path="/x/items")
        # 合法的逻辑数组元素路径应被接受（只要字段存在）。
        c = MetaCatalog()
        c.register_schema(
            "S", "1.0",
            obj(tags={"type": "array", "items": {"type": "string"}}),
        )
        result = c.trace_field_lineage("S", "1.0", "1.0", "/tags/-")
        self.assertEqual(result["target_path"], "/tags/-")

    def test_missing_start_version_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.c.trace_field_lineage("Person", "0.9", "2.0", "/age")

    def test_missing_field_is_not_found_even_with_valid_pointer(self):
        with self.assertRaises(NotFoundError) as cm:
            self.c.trace_field_lineage("Person", "1.0", "2.0", "/ghost")
        self.assertEqual(cm.exception.details["path"], "/ghost")

    def test_unknown_target_version_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.c.trace_field_lineage("Person", "1.0", "42.0", "/age")


class ReadOnlyAndDeterminismTests(unittest.TestCase):
    def setUp(self):
        self.c, _, _ = build_chain_catalog()

    def test_result_is_independent_copy(self):
        r1 = self.c.trace_field_lineage("Person", "1.0", "3.0", "/age")
        r1["steps"][0]["new_path"] = "/hacked"
        r1["target_path"] = "/hacked"
        r2 = self.c.trace_field_lineage("Person", "1.0", "3.0", "/age")
        self.assertEqual(r2["target_path"], "/years")
        self.assertEqual(r2["steps"][0]["new_path"], "/years")
        # 深拷贝：r1 的突变不影响 r2。
        self.assertEqual(r1["steps"][0]["new_path"], "/hacked")

    def test_deterministic_across_calls(self):
        r1 = self.c.trace_field_lineage("Person", "1.0", "3.0", "/age")
        r2 = self.c.trace_field_lineage("Person", "1.0", "3.0", "/age")
        self.assertEqual(r1, r2)

    def test_does_not_register_generate_report_or_write_index(self):
        before_reports = self.c.list_reports()
        before_search = self.c.search("years")
        self.c.trace_field_lineage("Person", "1.0", "3.0", "/age")
        self.c.trace_field_lineage("Person", "1.0", "3.0", "/age")
        self.assertEqual(self.c.list_reports(), before_reports)
        self.assertEqual(self.c.search("years"), before_search)
        # 未注册版本仍然未注册。
        self.assertEqual(self.c.list_versions("Person"), ["1.0", "2.0", "3.0"])

    def test_other_entrypoints_error_semantics_unchanged(self):
        # 非法比较输入仍抛 SchemaComparisonInvalid，血缘新增错误不影响既有口径。
        from meta_catalog.errors import SchemaComparisonInvalid
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.compare_schemas("Person", "1.0", None)


if __name__ == "__main__":
    unittest.main()
