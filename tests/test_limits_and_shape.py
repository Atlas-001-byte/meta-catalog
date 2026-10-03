import unittest

from meta_catalog import MetaCatalog, limits
from meta_catalog.errors import ImpactAnalysisTooLarge


class LimitTests(unittest.TestCase):
    def test_too_many_fields(self):
        c = MetaCatalog()
        old_props = {f"f{i}": {"type": "string"} for i in range(5)}
        c.register_schema("Big", "1.0", {"type": "object", "properties": old_props})
        old_limit = limits.MAX_FIELDS
        limits.MAX_FIELDS = 3
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                c.compare_schemas("Big", "1.0", {"type": "object", "properties": old_props})
            self.assertEqual(cm.exception.details["reason"], "fields_exceeded")
        finally:
            limits.MAX_FIELDS = old_limit

    def test_too_many_changes(self):
        c = MetaCatalog()
        old_props = {f"f{i}": {"type": "string"} for i in range(5)}
        c.register_schema("Big", "1.0", {"type": "object", "properties": old_props})
        # 删除全部字段 -> 5 条变更
        old_limit = limits.MAX_CHANGES
        limits.MAX_CHANGES = 3
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                c.compare_schemas("Big", "1.0", {"type": "object"})
            self.assertEqual(cm.exception.details["reason"], "changes_exceeded")
        finally:
            limits.MAX_CHANGES = old_limit

    def test_impact_chain_depth_limit(self):
        c = MetaCatalog()
        c.register_schema("S0", "1.0", {"type": "object", "properties": {"x": {"type": "string"}}})
        # 构造 S1 -> S0, S2 -> S1 ... 链式引用
        for i in range(1, 6):
            doc = {
                "type": "object",
                "properties": {"nested": {"$ref": f"S{i-1}@1.0#"}},
            }
            c.register_schema(f"S{i}", "1.0", doc)
        # 资产引用链末端字段，正向解析需沿 5 条边到达 S0:/x
        c.register_asset(
            "svc-deep", "深层服务", "service",
            [{"schema": "S5", "version": "1.0",
              "path": "/" + "/".join(["nested"] * 5 + ["x"])}],
        )
        old = limits.MAX_IMPACT_DEPTH
        limits.MAX_IMPACT_DEPTH = 2
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                c.analyze_impact("S0", "1.0", "/x")
            self.assertEqual(cm.exception.details["reason"], "depth_exceeded")
        finally:
            limits.MAX_IMPACT_DEPTH = old


class RenameSubtreeTests(unittest.TestCase):
    def test_renamed_subtree_partial_delete_and_add(self):
        c = MetaCatalog()
        v1 = {
            "type": "object",
            "properties": {
                "a": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "string"},
                        "y": {"type": "string"},
                    },
                }
            },
        }
        v2 = {
            "type": "object",
            "properties": {
                "b": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "string"},   # 对齐保留
                        "z": {"type": "string"},   # 新增
                    },
                }
            },
        }
        c.register_schema("Doc", "1.0", v1)
        rep = c.compare_schemas(
            "Doc", "1.0", v2, renames=[{"from": "/a", "to": "/b"}]
        )
        rows = {(ch["old_path"], ch["new_path"], ch["change_kind"]) for ch in rep["changes"]}
        # 根恰一次 rename
        self.assertIn(("/a", "/b", "rename"), rows)
        # 未被映射覆盖的删除 /a/y 与新增 /b/z 仍分别呈现
        self.assertIn(("/a/y", None, "deleted"), rows)
        self.assertIn((None, "/b/z", "added"), rows)
        # 对齐保留的 x 不产生条目
        self.assertFalse(any(r[0] == "/a/x" for r in rows))

    def test_overlapping_rename_endpoints_rejected(self):
        from meta_catalog.errors import SchemaComparisonInvalid

        c = MetaCatalog()
        v1 = {"type": "object", "properties": {"a": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
        }}}
        v2 = {"type": "object", "properties": {"b": {
            "type": "object",
            "properties": {"y": {"type": "string"}},
        }}}
        c.register_schema("D", "1.0", v1)
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            c.compare_schemas(
                "D", "1.0", v2,
                renames=[{"from": "/a", "to": "/b"},
                         {"from": "/a/x", "to": "/b/y"}],
            )
        self.assertEqual(cm.exception.details["reason"], "rename_overlapping")


class ReportShapeTests(unittest.TestCase):
    def test_entry_contains_required_fields(self):
        c = MetaCatalog()
        c.register_schema("P", "1.0", {"type": "object", "properties": {
            "age": {"type": "integer"}}})
        rep = c.compare_schemas(
            "P", "1.0",
            {"type": "object", "properties": {"age": {"type": "number"}}},
            candidate_version="2.0",
        )
        (entry,) = rep["changes"]
        for key in (
            "path", "old_path", "new_path", "old_summary", "new_summary",
            "change_kind", "compatibility", "direct_assets", "transitive_assets",
        ):
            self.assertIn(key, entry)
        self.assertEqual(entry["old_summary"], {"type": "integer"})
        self.assertEqual(entry["new_summary"], {"type": "number"})
        self.assertEqual(rep["schema"], "P")
        self.assertEqual(rep["baseline_version"], "1.0")
        self.assertEqual(rep["candidate_version"], "2.0")


if __name__ == "__main__":
    unittest.main()
