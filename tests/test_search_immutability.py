import copy
import unittest

from meta_catalog import MetaCatalog
from meta_catalog.errors import AlreadyExistsError


def build_catalog():
    c = MetaCatalog()
    v1 = {
        "type": "object",
        "title": "Person schema",
        "properties": {
            "name": {"type": "string", "title": "姓名"},
            "age": {"type": "integer"},
            "level": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["name"],
    }
    c.register_schema("Person", "1.0", v1)
    c.register_asset(
        "svc-mail", "邮寄服务", "service",
        [{"schema": "Person", "version": "1.0", "path": "/name"}],
    )
    v2 = {
        "type": "object",
        "title": "Person schema",
        "properties": {
            "name": {"type": "string", "title": "姓名", "description": "全名"},
            "age": {"type": "number"},
            "level": {"type": "string", "enum": ["a"]},
            "nick": {"type": "string"},
        },
        "required": ["name", "age"],
    }
    c.compare_schemas("Person", "1.0", v2, candidate_version="2.0")
    return c, v1


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.c, self.v1 = build_catalog()

    def test_keyword_search_returns_schema_asset_change(self):
        hits = self.c.search("name")
        types = {h["type"] for h in hits}
        self.assertIn("schema", types)
        self.assertIn("asset", types)
        self.assertIn("change", types)

    def test_filter_by_change_kind_and_compatibility(self):
        hits = self.c.search(change_kind="modified", compatibility="breaking")
        self.assertTrue(hits)
        paths = {h["path"] for h in hits}
        # level 枚举收窄、age 新增必填，两者均为 breaking
        self.assertEqual(paths, {"/age", "/level"})
        for h in hits:
            self.assertEqual(h["change_kind"], "modified")
            self.assertEqual(h["compatibility"], "breaking")

    def test_filter_by_field_path(self):
        hits = self.c.search(field_path="/age")
        self.assertTrue(hits)
        for h in hits:
            self.assertTrue(
                h["path"] == "/age"
                or h.get("old_path") == "/age"
                or h.get("new_path") == "/age"
            )

    def test_filter_by_schema_and_version(self):
        hits = self.c.search(schema="Person", version="2.0")
        self.assertTrue(hits)
        for h in hits:
            self.assertEqual(h.get("schema", h.get("name")), "Person")

    def test_filter_by_asset_name_matches_impacted_changes(self):
        hits = self.c.search(asset_name="邮寄")
        self.assertTrue(hits)
        # /name 变更为 metadata，且直接影响邮寄服务 -> change 必须命中
        change_hits = [h for h in hits if h["type"] == "change" and h["path"] == "/name"]
        self.assertTrue(change_hits)
        self.assertIn("impact_assets", change_hits[0]["matched_fields"])

    def test_combined_filters(self):
        hits = self.c.search(
            "age", schema="Person", version="2.0",
            change_kind="modified",
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["path"], "/age")

    def test_result_stability(self):
        a = self.c.search()
        b = self.c.search()
        keys = [(h["type"], h.get("path") or h.get("id") or h.get("name")) for h in a]
        keys2 = [(h["type"], h.get("path") or h.get("id") or h.get("name")) for h in b]
        self.assertEqual(keys, keys2)

    def test_old_keyword_matching_unchanged_after_report(self):
        c = MetaCatalog()
        c.register_schema("Widget", "1.0", {"type": "object", "title": "alpha beta"})
        before = [(h["type"], h["name"]) for h in c.search("alpha")]
        self.assertEqual(before, [("schema", "Widget")])
        # 生成报告后，旧查询结果语义不变。
        c.compare_schemas(
            "Widget", "1.0",
            {"type": "object", "title": "alpha beta", "properties": {"x": {"type": "string"}}},
        )
        after = [(h["type"], h.get("name") or h.get("path")) for h in c.search("alpha")]
        self.assertIn(("schema", "Widget"), after)
        # schema 命中字段不变
        schema_hit = next(h for h in c.search("alpha") if h["type"] == "schema")
        self.assertEqual(schema_hit["matched_fields"], ["title"])


class ImmutabilityTests(unittest.TestCase):
    def test_registration_never_overwritten(self):
        c, v1 = build_catalog()
        with self.assertRaises(AlreadyExistsError):
            c.register_schema("Person", "1.0", {"type": "string"})

    def test_returned_documents_are_copies(self):
        c, v1 = build_catalog()
        got = c.get_schema("Person", "1.0")
        got["document"]["properties"]["hacked"] = {"type": "string"}
        again = c.get_schema("Person", "1.0")
        self.assertNotIn("hacked", again["document"]["properties"])

    def test_compare_and_search_do_not_mutate_registry(self):
        c, v1 = build_catalog()
        before = c.get_schema("Person", "1.0")
        c.compare_schemas(
            "Person", "1.0",
            {"type": "object", "properties": {"ghost": {"type": "string"}}},
        )
        c.search("ghost")
        after = c.get_schema("Person", "1.0")
        self.assertEqual(before["document"], after["document"])
        # 候选文档未注册：版本列表不变
        self.assertEqual(c.list_versions("Person"), ["1.0"])

    def test_input_document_not_retained_by_reference(self):
        c = MetaCatalog()
        doc = {"type": "object", "properties": {"a": {"type": "string"}}}
        c.register_schema("S", "1.0", doc)
        doc["properties"]["a"]["type"] = "integer"
        self.assertEqual(
            c.get_schema("S", "1.0")["document"]["properties"]["a"]["type"], "string"
        )


if __name__ == "__main__":
    unittest.main()
