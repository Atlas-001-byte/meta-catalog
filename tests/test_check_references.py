import json
import unittest

from meta_catalog import MetaCatalog
from meta_catalog.errors import AlreadyExistsError, NotFoundError


def schema(**props):
    return {"type": "object", "properties": props}


class CheckReferencesTests(unittest.TestCase):
    def test_no_references_counts_as_resolved(self):
        c = MetaCatalog()
        c.register_schema("Plain", "1.0", schema(a={"type": "string"}))
        out = c.check_schema_references()
        self.assertEqual(out["checked"], 1)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["resolved"], 1)
        self.assertEqual(out["issues"], [])
        self.assertEqual(out["references"], [])

    def test_all_resolved(self):
        c = MetaCatalog()
        c.register_schema("Address", "1.0", schema(street={"type": "string"}))
        c.register_schema(
            "Person", "1.0",
            schema(addr={"$ref": "Address@1.0#"}),
        )
        out = c.check_schema_references()
        self.assertEqual((out["checked"], out["total"], out["resolved"]), (2, 2, 2))
        self.assertEqual(out["issues"], [])
        self.assertEqual(len(out["references"]), 1)
        ref = out["references"][0]
        self.assertEqual(
            ref,
            {
                "source_schema": "Person",
                "source_version": "1.0",
                "source_path": "/addr",
                "target_schema": "Address",
                "target_version": "1.0",
                "target_path": "",
                "status": "resolved",
            },
        )

    def test_missing_schema(self):
        c = MetaCatalog()
        c.register_schema("Person", "1.0", schema(g={"$ref": "Ghost@1.0#"}))
        out = c.check_schema_references()
        self.assertEqual(out["resolved"], 0)
        self.assertEqual(out["references"][0]["status"], "missing_schema")
        self.assertEqual(len(out["issues"]), 1)
        issue = out["issues"][0]
        self.assertEqual(issue["reason"], "missing_schema")
        self.assertEqual(issue["source_schema"], "Person")
        self.assertEqual(issue["target_schema"], "Ghost")
        self.assertTrue(issue["message"])

    def test_missing_field_forward_ref(self):
        c = MetaCatalog()
        c.register_schema(
            "Person", "1.0",
            schema(addr={"$ref": "Addr@1.0#/properties/nope"}),
        )
        c.register_schema("Addr", "1.0", schema(street={"type": "string"}))
        out = c.check_schema_references()
        self.assertEqual(out["resolved"], 1)  # Addr 自身无引用，计入 resolved
        self.assertEqual(out["references"][0]["status"], "missing_field")
        self.assertEqual(out["references"][0]["target_path"], "/nope")
        self.assertEqual(out["issues"][0]["reason"], "missing_field")

    def test_invalid_pointer(self):
        c = MetaCatalog()
        c.register_schema("P1", "1.0", schema(a={"$ref": "Addr@1.0#street"}))
        c.register_schema("P2", "1.0", schema(b={"$ref": "Addr@1.0#/properties/a~b"}))
        c.register_schema("Addr", "1.0", schema(street={"type": "string"}))
        out = c.check_schema_references()
        statuses = {r["source_schema"]: r["status"] for r in out["references"]}
        self.assertEqual(statuses, {"P1": "invalid_pointer", "P2": "invalid_pointer"})
        by_source = {r["source_schema"]: r for r in out["references"]}
        self.assertEqual(by_source["P1"]["target_path"], "street")
        self.assertEqual({i["reason"] for i in out["issues"]}, {"invalid_pointer"})

    def test_invalid_pointer_masked_by_missing_schema(self):
        # 目标不存在时优先记 missing_schema。
        c = MetaCatalog()
        c.register_schema("P", "1.0", schema(a={"$ref": "Ghost@1.0#street"}))
        out = c.check_schema_references()
        self.assertEqual(out["references"][0]["status"], "missing_schema")

    def test_root_array_and_additional_properties_paths(self):
        c = MetaCatalog()
        c.register_schema(
            "T", "1.0",
            schema(
                tags={"type": "array", "items": {"type": "string"}},
                meta={
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            ),
        )
        c.register_schema(
            "S", "1.0",
            schema(
                a={"$ref": "T@1.0#/properties/tags/items"},
                b={"$ref": "T@1.0#/properties/meta/additionalProperties"},
            ),
        )
        out = c.check_schema_references("S", "1.0")
        self.assertEqual(out["resolved"], 1)
        paths = {r["source_path"]: r["target_path"] for r in out["references"]}
        self.assertEqual(paths, {"/a": "/tags/-", "/b": "/meta/*"})

    def test_resolved_through_ref_chain(self):
        c = MetaCatalog()
        c.register_schema("C", "1.0", schema(bar={"type": "string"}))
        # A 在 B 之前注册（前向引用），B.foo 引用 C 根。
        c.register_schema("A", "1.0", schema(x={"$ref": "B@1.0#/properties/foo/bar"}))
        c.register_schema("B", "1.0", schema(foo={"$ref": "C@1.0#"}))
        out = c.check_schema_references("A")
        self.assertEqual(out["resolved"], 1)
        self.assertEqual(out["issues"], [])
        self.assertEqual(out["references"][0]["status"], "resolved")
        self.assertEqual(out["references"][0]["target_path"], "/foo/bar")

    def test_cycle_terminates(self):
        c = MetaCatalog()
        c.register_schema("A", "1.0", schema(b={"$ref": "B@1.0#"}, x={"type": "string"}))
        c.register_schema("B", "1.0", schema(a={"$ref": "A@1.0#"}, y={"type": "string"}))
        out = c.check_schema_references()
        self.assertEqual(out["resolved"], 2)
        self.assertEqual(out["issues"], [])
        # 环上字段路径不可达时记 missing_field，且必须终止（坏引用须为前向引用）。
        c2 = MetaCatalog()
        c2.register_schema("B", "1.0", schema(a={"$ref": "A@1.0#/properties/nope"}))
        c2.register_schema("A", "1.0", schema(b={"$ref": "B@1.0#"}))
        out2 = c2.check_schema_references()
        self.assertEqual(
            sorted(r["status"] for r in out2["references"]),
            ["missing_field", "resolved"],
        )

    def test_internal_refs_not_checked(self):
        c = MetaCatalog()
        c.register_schema(
            "Doc", "1.0",
            {
                "type": "object",
                "definitions": {"s": {"type": "string"}},
                "properties": {"a": {"$ref": "#/definitions/s"}},
            },
        )
        out = c.check_schema_references()
        self.assertEqual(out["references"], [])
        self.assertEqual(out["resolved"], 1)

    def test_filter_by_name_uses_all_its_versions(self):
        c = MetaCatalog()
        c.register_schema("A", "1.0", schema(x={"type": "string"}))
        c.register_schema("A", "2.0", schema(x={"type": "integer"}))
        c.register_schema("B", "1.0", schema(g={"$ref": "Ghost@1.0#"}))
        out = c.check_schema_references("A")
        self.assertEqual((out["checked"], out["total"], out["resolved"]), (2, 2, 2))
        out_b = c.check_schema_references("B")
        self.assertEqual((out_b["checked"], out_b["resolved"]), (1, 0))

    def test_filter_by_version_only(self):
        c = MetaCatalog()
        c.register_schema("A", "1.0", schema(x={"type": "string"}))
        c.register_schema("A", "2.0", schema(x={"type": "integer"}))
        c.register_schema("B", "1.0", schema(g={"$ref": "Ghost@1.0#"}))
        out = c.check_schema_references(version="1.0")
        self.assertEqual((out["checked"], out["total"]), (2, 2))
        self.assertEqual(out["resolved"], 1)
        out2 = c.check_schema_references(version="2.0")
        self.assertEqual((out2["checked"], out2["resolved"]), (1, 1))

    def test_filter_by_name_and_version(self):
        c = MetaCatalog()
        c.register_schema("A", "1.0", schema(x={"type": "string"}))
        c.register_schema("A", "2.0", schema(g={"$ref": "Ghost@1.0#"}))
        out = c.check_schema_references("A", "2.0")
        self.assertEqual((out["checked"], out["resolved"]), (1, 0))

    def test_no_match_raises_not_found(self):
        c = MetaCatalog()
        with self.assertRaises(NotFoundError):
            c.check_schema_references()
        c.register_schema("A", "1.0", schema(x={"type": "string"}))
        with self.assertRaises(NotFoundError):
            c.check_schema_references("Unknown")
        with self.assertRaises(NotFoundError):
            c.check_schema_references("A", "9.9")
        with self.assertRaises(NotFoundError):
            c.check_schema_references(version="9.9")

    def test_issues_sorted_and_result_stable_and_json_serializable(self):
        c = MetaCatalog()
        c.register_schema(
            "S", "1.0",
            schema(
                b={"$ref": "Ghost@2.0#"},
                a={"$ref": "Ghost@1.0#"},
            ),
        )
        out1 = c.check_schema_references()
        out2 = c.check_schema_references()
        self.assertEqual(out1, out2)
        keys = [
            (i["source_schema"], i["source_version"], i["source_path"],
             i["target_schema"], i["target_version"], i["target_path"])
            for i in out1["issues"]
        ]
        self.assertEqual(keys, sorted(keys))
        json.dumps(out1)

    def test_audit_is_readonly_and_not_indexed(self):
        c = MetaCatalog()
        c.register_schema("Address", "1.0", schema(street={"type": "string"}))
        c.register_schema("Person", "1.0", schema(g={"$ref": "Ghost@1.0#"}))
        before_search = c.search("Person")
        before_schema = c.get_schema("Person", "1.0")
        c.check_schema_references()
        c.check_schema_references("Person")
        self.assertEqual(c.search("Person"), before_search)
        self.assertEqual(c.get_schema("Person", "1.0"), before_schema)
        # 审计结果不出现在检索中。
        self.assertEqual(c.search("Ghost"), [])
        self.assertEqual(c.search("missing_schema"), [])

    def test_duplicate_registration_still_rejected(self):
        c = MetaCatalog()
        c.register_schema("A", "1.0", schema(x={"type": "string"}))
        with self.assertRaises(AlreadyExistsError):
            c.register_schema("A", "1.0", schema(x={"type": "string"}))
        out = c.check_schema_references()
        self.assertEqual((out["checked"], out["resolved"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
