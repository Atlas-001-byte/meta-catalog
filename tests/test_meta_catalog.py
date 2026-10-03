"""元数据目录：注册 / 影响 / 比较 / 检索 测试。"""

import unittest

from meta_catalog import (
    Catalog,
    ChangeKind,
    Compatibility,
    ImpactAnalysisTooLarge,
    SchemaComparisonInvalid,
)
from meta_catalog.limits import LIMITS


def obj(properties=None, required=None, **kw):
    d = {"type": "object"}
    if properties is not None:
        d["properties"] = properties
    if required is not None:
        d["required"] = required
    d.update(kw)
    return d


def s(t, **kw):
    d = {"type": t}
    d.update(kw)
    return d


def kinds(report):
    return {c["field_path"]: (c["kind"], c["compatibility"]) for c in report["changes"]}


def entries(report):
    return {c["field_path"]: c for c in report["changes"]}


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.c = Catalog()

    def compare(self, old, new, renames=None):
        self.c.register_schema("S", "1", old)
        self.c.register_schema("S", "2", new)
        return self.c.compare_schemas("S", "1", "2", renames=renames)

    def test_added_optional_is_compatible(self):
        rep = self.compare(obj({"a": s("integer")}), obj({"a": s("integer"), "b": s("string")}))
        self.assertEqual(kinds(rep)["/b"], ("added", "compatible"))

    def test_added_required_is_breaking(self):
        rep = self.compare(obj({"a": s("integer")}), obj({"a": s("integer"), "b": s("string")}, ["a", "b"]))
        self.assertEqual(kinds(rep)["/b"], ("added_required", "breaking"))

    def test_removed_is_breaking(self):
        rep = self.compare(obj({"a": s("integer"), "b": s("string")}), obj({"a": s("integer")}))
        self.assertEqual(kinds(rep)["/b"], ("removed", "breaking"))

    def test_required_added_is_breaking(self):
        rep = self.compare(obj({"a": s("integer")}, []), obj({"a": s("integer")}, ["a"]))
        self.assertEqual(kinds(rep)["/a"], ("required_added", "breaking"))

    def test_required_relaxed_is_compatible(self):
        rep = self.compare(obj({"a": s("integer")}, ["a"]), obj({"a": s("integer")}, []))
        self.assertEqual(kinds(rep)["/a"], ("required_relaxed", "compatible"))

    def test_enum_relaxed_and_narrowed(self):
        rep = self.compare(
            obj({"x": s("string", enum=["A", "B"])}),
            obj({"x": s("string", enum=["A", "B", "C"])}),
        )
        self.assertEqual(kinds(rep)["/x"], ("enum_relaxed", "compatible"))
        c2 = Catalog()
        c2.register_schema("S", "1", obj({"y": s("string", enum=["A", "B", "C"])}))
        c2.register_schema("S", "2", obj({"y": s("string", enum=["A"])}))
        rep2 = c2.compare_schemas("S", "1", "2")
        self.assertEqual(kinds(rep2)["/y"], ("enum_narrowed", "breaking"))

    def test_integer_to_number_widened(self):
        rep = self.compare(obj({"x": s("integer")}), obj({"x": s("number")}))
        self.assertEqual(kinds(rep)["/x"], ("type_widened", "compatible"))

    def test_number_to_integer_narrowed(self):
        rep = self.compare(obj({"x": s("number")}), obj({"x": s("integer")}))
        self.assertEqual(kinds(rep)["/x"], ("type_narrowed", "breaking"))

    def test_nullable_added_is_breaking(self):
        rep = self.compare(obj({"x": s("string")}), obj({"x": s(["string", "null"])}))
        self.assertEqual(kinds(rep)["/x"], ("nullable_added", "breaking"))

    def test_default_added_is_compatible(self):
        rep = self.compare(obj({"x": s("integer")}), obj({"x": s("integer", default=0)}))
        self.assertEqual(kinds(rep)["/x"], ("default_added", "compatible"))

    def test_metadata_only_change(self):
        rep = self.compare(
            obj({"x": s("string", title="Old", description="d1")}),
            obj({"x": s("string", title="New", description="d2", **{"$comment": "c"})}),
        )
        self.assertEqual(kinds(rep)["/x"], ("metadata_changed", "metadata"))

    def test_bounds_narrow_and_relax(self):
        rep = self.compare(
            obj({"x": s("integer", minimum=0)}),
            obj({"x": s("integer", minimum=10)}),
        )
        self.assertEqual(kinds(rep)["/x"], ("constraint_narrowed", "breaking"))
        c2 = Catalog()
        c2.register_schema("S", "1", obj({"y": s("integer", maximum=10)}))
        c2.register_schema("S", "2", obj({"y": s("integer", maximum=20)}))
        rep2 = c2.compare_schemas("S", "1", "2")
        self.assertEqual(kinds(rep2)["/y"], ("constraint_relaxed", "compatible"))

    def test_pattern_added_narrows(self):
        rep = self.compare(obj({"x": s("string")}), obj({"x": s("string", pattern="^a")}))
        self.assertEqual(kinds(rep)["/x"], ("constraint_narrowed", "breaking"))

    def test_no_changes(self):
        doc = obj({"a": s("integer")}, ["a"])
        rep = self.compare(doc, obj({"a": s("integer")}, ["a"]))
        self.assertEqual(rep["changes"], [])

    def test_enum_disjoint_is_breaking(self):
        rep = self.compare(
            obj({"x": s("string", enum=["A", "B"])}),
            obj({"x": s("string", enum=["C", "D"])}),
        )
        self.assertEqual(kinds(rep)["/x"], ("enum_narrowed", "breaking"))

    def test_format_change_is_compatible(self):
        rep = self.compare(
            obj({"x": s("string", format="email")}),
            obj({"x": s("string", format="uri")}),
        )
        self.assertEqual(kinds(rep)["/x"], ("constraint_relaxed", "compatible"))

    def test_unconstrained_to_typed_is_narrowed(self):
        rep = self.compare(obj({"x": {}}), obj({"x": s("string")}))
        self.assertEqual(kinds(rep)["/x"], ("type_narrowed", "breaking"))

    def test_nullable_without_prior_types_not_flagged(self):
        # 旧版本没有类型约束（本就接受 null），不应计为 nullable_added
        rep = self.compare(obj({"x": {}}), obj({"x": s(["string", "null"])}))
        self.assertNotEqual(kinds(rep)["/x"][0], "nullable_added")

    def test_self_rename_rejected(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.compare(obj({"a": s("integer")}), obj({"a": s("integer")}), renames=[("a", "a")])

    def test_nested_paths_and_arrays(self):
        old = obj({"child": obj({"x": s("integer")}), "arr": {"type": "array", "items": s("integer")}})
        new = obj({"child": obj({"x": s("number")}), "arr": {"type": "array", "items": s("number")}})
        rep = self.compare(old, new)
        k = kinds(rep)
        self.assertEqual(k["/child/x"], ("type_widened", "compatible"))
        self.assertEqual(k["/arr/[]"], ("type_widened", "compatible"))


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.c = Catalog()
        self.old = obj({"a": s("integer"), "keep": s("string"), "gone": s("integer")})
        self.new = obj({"b": s("integer"), "keep": s("string"), "extra": s("string")})
        self.c.register_schema("S", "1", self.old)
        self.c.register_schema("S", "2", self.new)

    def test_rename_single_event(self):
        rep = self.c.compare_schemas("S", "1", "2", renames=[{"from": "/a", "to": "/b"}])
        k = kinds(rep)
        self.assertEqual(k["/a"], ("renamed", "breaking"))
        self.assertNotIn("/b", k)  # 不再同时计为新增
        self.assertEqual(k["/gone"], ("removed", "breaking"))
        self.assertEqual(k["/extra"], ("added", "compatible"))
        entry = entries(rep)["/a"]
        self.assertEqual(entry["renamed_to"], "/b")

    def test_rename_accepts_tuple_and_dashless_path(self):
        rep = self.c.compare_schemas("S", "1", "2", renames=[("a", "b")])
        self.assertEqual(kinds(rep)["/a"], ("renamed", "breaking"))

    def test_rename_source_missing(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.compare_schemas("S", "1", "2", renames=[{"from": "/nope", "to": "/b"}])

    def test_rename_target_missing(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.compare_schemas("S", "1", "2", renames=[{"from": "/a", "to": "/nope"}])

    def test_duplicate_source(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.compare_schemas(
                "S", "1", "2",
                renames=[{"from": "/a", "to": "/b"}, {"from": "/a", "to": "/extra"}],
            )

    def test_duplicate_target(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.compare_schemas(
                "S", "1", "2",
                renames=[{"from": "/a", "to": "/extra"}, {"from": "/gone", "to": "/extra"}],
            )

    def test_rename_into_existing_path_keeps_old_identity_removed(self):
        # a 改名成已存在的 keep：一次 rename，同时旧 keep 身份在新侧删除。
        rep = self.c.compare_schemas("S", "1", "2", renames=[{"from": "/a", "to": "/keep"}])
        k = kinds(rep)
        self.assertEqual(k["/a"], ("renamed", "breaking"))
        self.assertEqual(entries(rep)["/a"]["renamed_to"], "/keep")
        self.assertEqual(k["/keep"], ("removed", "breaking"))

    def test_rename_chain_across_versions(self):
        # 连续版本的链式重命名 b -> c -> d 与历史一致。
        c = Catalog()
        v1 = obj({"a": s("integer"), "b": s("integer")})
        v2 = obj({"a": s("integer"), "c": s("integer")})
        v3 = obj({"a": s("integer"), "d": s("integer")})
        for v, d in [("1", v1), ("2", v2), ("3", v3)]:
            c.register_schema("S", v, d)
        c.compare_schemas("S", "1", "2", renames=[{"from": "/b", "to": "/c"}])
        rep_ok = c.compare_schemas("S", "2", "3", renames=[{"from": "/c", "to": "/d"}])
        self.assertEqual(kinds(rep_ok)["/c"], ("renamed", "breaking"))

    def test_cross_version_inconsistent(self):
        # 历史：1->2 中 /b 改名为 /c。
        c = Catalog()
        v1 = obj({"a": s("integer"), "b": s("integer")})
        v2 = obj({"a": s("integer"), "c": s("integer")})
        v2b = obj({"a": s("integer"), "b": s("integer"), "c": s("integer")})
        for v, d in [("1", v1), ("2", v2), ("2b", v2b)]:
            c.register_schema("S", v, d)
        c.compare_schemas("S", "1", "2", renames=[{"from": "/b", "to": "/c"}])
        # 2->2b：/c 改名成 /b，与历史身份链 b->c->b 仍自洽。
        c.compare_schemas("S", "2", "2b", renames=[{"from": "/c", "to": "/b"}])
        # 再声称 1->2b：/b 改名成 /c。此时同一身份在 2b 上同时对应
        # /b 与 /c 两条路径 -> 跨版本不一致。
        with self.assertRaises(SchemaComparisonInvalid):
            c.compare_schemas("S", "1", "2b", renames=[{"from": "/b", "to": "/c"}])


class ErrorTests(unittest.TestCase):
    def setUp(self):
        self.c = Catalog()
        self.c.register_schema("S", "1", obj({"a": s("integer")}))

    def test_invalid_candidate_inline(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.compare_schemas("S", "1", "x", candidate_document='{"type": oops}')

    def test_baseline_version_missing(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.compare_schemas("S", "9", "1")

    def test_candidate_version_missing(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.compare_schemas("S", "1", "9")

    def test_candidate_must_be_object_schema(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.compare_schemas("S", "1", "2", candidate_document=[1, 2, 3])

    def test_invalid_enum_filter(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.search_changes(kind="not_a_kind")


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.c = Catalog()
        # User 与 Order：Order.lineItems[].buyer 引用 User
        user_v1 = obj({"id": s("integer"), "name": s("string")}, ["id"])
        user_v2 = obj({"id": s("integer"), "full_name": s("string")}, ["id"])
        self.c.register_schema("User", "1", user_v1)
        self.c.register_schema("User", "2", user_v2)
        order_v1 = obj({
            "orderId": s("integer"),
            "buyer": {"$ref": "catalog://User@1#/"},
        })
        order_v2 = obj({
            "orderId": s("integer"),
            "buyer": {"$ref": "catalog://User@2#/"},
        })
        self.c.register_schema("Order", "1", order_v1)
        self.c.register_schema("Order", "2", order_v2)
        # 发票又引用 Order（传递链第二层）
        invoice = obj({
            "order": {"$ref": "catalog://Order@1#/"},
            "invoiceNo": s("string"),
        })
        self.c.register_schema("Invoice", "1", invoice)

        self.c.register_asset(
            "asset-direct", "User Pipeline", "pipeline",
            [{"schema": "User", "version": "1", "field": "/name"}],
        )
        self.c.register_asset(
            "asset-order", "Order Service", "service",
            [{"schema": "Order", "version": "1", "field": "/buyer"}],
        )
        self.c.register_asset(
            "asset-invoice", "Invoice Job", "job",
            [{"schema": "Invoice", "version": "1", "field": "/order"}],
        )

    def test_direct_and_transitive_impact(self):
        rep = self.c.compare_schemas(
            "User", "1", "2",
            renames=[{"from": "/name", "to": "/full_name"}],
        )
        entry = entries(rep)["/name"]
        names = [(a["name"], a["depth"]) for a in entry["impacted_assets"]]
        self.assertIn(("User Pipeline", 1), names)
        self.assertIn(("Order Service", 2), names)
        self.assertIn(("Invoice Job", 3), names)
        # 链稳定、路径明确
        invoice_hit = [a for a in entry["impacted_assets"] if a["name"] == "Invoice Job"][0]
        chain_schemas = [n["schema"] for n in invoice_hit["chain"]]
        self.assertEqual(chain_schemas, ["User", "Order", "Invoice"])

    def test_subtree_overlap(self):
        # 资产引用整个 User 根，字段级变更也应命中
        c = Catalog()
        c.register_schema("U", "1", obj({"a": s("integer")}))
        c.register_schema("U", "2", obj({"a": s("number")}))
        c.register_asset("x", "Root User", "table", [{"schema": "U", "version": "1"}])
        rep = c.compare_schemas("U", "1", "2")
        hit = entries(rep)["/a"]["impacted_assets"]
        self.assertEqual([a["name"] for a in hit], ["Root User"])

    def test_impact_dedup_when_multiple_paths(self):
        out = self.c.impact_analysis("User", "1", ["/name"])
        field = out["fields"][0]
        ids = [a["asset_id"] for a in field["impacted_assets"]]
        self.assertEqual(len(ids), len(set(ids)))


class DeterminismTests(unittest.TestCase):
    def test_identical_inputs_identical_report(self):
        def build():
            c = Catalog()
            old = {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "z": {"type": "integer"},
                    "a": {"type": "string", "enum": ["x", "y"]},
                    "m": {"type": "object", "properties": {"p": {"type": "integer"}}},
                },
            }
            new = {
                "type": "object",
                "required": ["id", "z"],
                "properties": {
                    "z": {"type": "number"},
                    "a": {"type": "string", "enum": ["x", "y", "z"]},
                    "m": {"type": "object", "properties": {"p": {"type": "number"}, "q": {"type": "string"}}},
                },
            }
            c.register_schema("S", "1", old)
            c.register_schema("S", "2", new)
            c.register_asset("a1", "A", "t", [{"schema": "S", "version": "1", "field": "/a"}])
            c.register_asset("a2", "B", "t", [{"schema": "S", "version": "1", "field": "/m/p"}])
            return c.compare_schemas("S", "1", "2")

        r1 = build()
        r2 = build()
        self.assertEqual(r1["report_id"], r2["report_id"])
        self.assertEqual(
            [(c["field_path"], c["kind"]) for c in r1["changes"]],
            [(c["field_path"], c["kind"]) for c in r2["changes"]],
        )

    def test_rename_order_independent(self):
        c1, c2 = Catalog(), Catalog()
        old = obj({"a": s("integer"), "b": s("integer")})
        new = obj({"x": s("integer"), "y": s("integer")})
        for c in (c1, c2):
            c.register_schema("S", "1", old)
            c.register_schema("S", "2", new)
        r1 = c1.compare_schemas("S", "1", "2", renames=[("a", "x"), ("b", "y")])
        r2 = c2.compare_schemas("S", "1", "2", renames=[("b", "y"), ("a", "x")])
        self.assertEqual(r1["report_id"], r2["report_id"])
        self.assertEqual(
            [ch["field_path"] for ch in r1["changes"]],
            [ch["field_path"] for ch in r2["changes"]],
        )


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.c = Catalog()
        old = obj({"id": s("integer"), "name": s("string")})
        new = obj({"id": s("integer"), "full_name": s("string")}, ["full_name"])
        self.c.register_schema("User", "1.0.0", old)
        self.c.register_schema("User", "2.0.0", new)
        self.c.register_asset(
            "p1", "User Warehouse", "table",
            [{"schema": "User", "version": "1.0.0", "field": "/name"}],
            "stores users",
        )
        self.report = self.c.compare_schemas(
            "User", "1.0.0", "2.0.0",
            renames=[{"from": "/name", "to": "/full_name"}],
        )

    def test_old_asset_query_semantics_unchanged(self):
        hits = self.c.search_assets("user warehouse")
        self.assertEqual([h["asset_id"] for h in hits], ["p1"])
        self.assertEqual(self.c.search_assets("nonexistent token"), [])
        # 空查询返回全部，顺序稳定
        again = self.c.search_assets("")
        self.assertEqual([h["asset_id"] for h in again], ["p1"])

    def test_search_by_schema_and_kind(self):
        res = self.c.search_changes(schema="User", kind="renamed")
        self.assertEqual(res["total"], 1)
        fields = res["hits"][0]["matched_fields"]
        self.assertEqual([f["field_path"] for f in fields], ["/name"])
        self.assertIn("renamed_to", fields[0])

    def test_search_by_version(self):
        self.assertEqual(self.c.search_changes(version="1.0.0")["total"], 1)
        self.assertEqual(self.c.search_changes(version="9.9.9")["total"], 0)

    def test_search_by_field_path(self):
        res = self.c.search_changes(field_path="name")
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["hits"][0]["matched_fields"][0]["field_path"], "/name")

    def test_search_by_compatibility(self):
        res = self.c.search_changes(compatibility=Compatibility.BREAKING.value)
        paths = [f["field_path"] for f in res["hits"][0]["matched_fields"]]
        # rename 以旧路径呈现；/full_name 是其 renamed_to，不另计新增
        self.assertEqual(paths, ["/name"])

    def test_search_by_impacted_asset_returns_summary(self):
        res = self.c.search_changes(impacted_asset="warehouse")
        self.assertEqual(res["total"], 1)
        assets = res["hits"][0]["matched_assets"]
        self.assertEqual(assets[0]["name"], "User Warehouse")
        self.assertEqual(assets[0]["depth"], 1)

    def test_combined_filters_and_stable_order(self):
        # 按 rename 终点路径片段 "full" 也能命中旧路径 /name 的 rename 条目
        res = self.c.search_changes(
            schema="user", version="2.0.0",
            compatibility="breaking", field_path="full",
        )
        self.assertEqual(
            [f["field_path"] for f in res["hits"][0]["matched_fields"]],
            ["/name"],
        )
        self.assertEqual(res["hits"][0]["matched_fields"][0]["renamed_to"], "/full_name")

    def test_report_read_back(self):
        fetched = self.c.get_report(self.report["report_id"])
        self.assertEqual(fetched["schema"], "User")
        self.assertEqual(
            self.c.list_reports()[0]["change_count"], len(self.report["changes"])
        )


class ImmutabilityTests(unittest.TestCase):
    def test_compare_does_not_mutate_registration(self):
        c = Catalog()
        old = obj({"a": s("integer")})
        c.register_schema("S", "1", old)
        c.register_schema("S", "2", obj({"a": s("number"), "b": s("string")}))
        before = c.impact_analysis("S", "1", ["/a"])
        r1 = c.compare_schemas("S", "1", "2")
        r2 = c.compare_schemas("S", "1", "2")  # 重复比较：幂等去重
        after = c.impact_analysis("S", "1", ["/a"])
        self.assertEqual(before, after)
        self.assertEqual(r1["report_id"], r2["report_id"])
        self.assertEqual(len(c.list_reports()), 1)

    def test_inline_candidate_not_registered(self):
        c = Catalog()
        c.register_schema("S", "1", obj({"a": s("integer")}))
        c.compare_schemas("S", "1", "9-temp", candidate_document=obj({"a": s("number")}))
        # 候选版本仍不可作为注册版本读取
        from meta_catalog.errors import CatalogError
        with self.assertRaises(CatalogError):
            c.impact_analysis("S", "9-temp")

    def test_registered_document_returns_summary_copies(self):
        c = Catalog()
        c.register_schema("S", "1", obj({"a": s("integer")}))
        c.register_schema("S", "2", obj({"a": s("number")}))
        rep = c.compare_schemas("S", "1", "2")
        rep["changes"][0]["kind"] = "tampered"
        again = c.get_report(rep["report_id"])
        self.assertNotEqual(again["changes"][0]["kind"], "tampered")


class LimitTests(unittest.TestCase):
    def test_too_many_fields(self):
        original = LIMITS["max_fields_per_comparison"]
        try:
            LIMITS["max_fields_per_comparison"] = 3
            c = Catalog()
            c.register_schema("S", "1", obj({k: s("integer") for k in ["a", "b", "c"]}))
            c.register_schema("S", "2", obj({k: s("integer") for k in ["a", "b", "c", "d"]}))
            with self.assertRaises(ImpactAnalysisTooLarge):
                c.compare_schemas("S", "1", "2")
        finally:
            LIMITS["max_fields_per_comparison"] = original

    def test_impact_chain_too_deep(self):
        original = LIMITS["max_impact_depth"]
        try:
            LIMITS["max_impact_depth"] = 2
            c = Catalog()
            # A <- B <- C <- D（三层反向边，超过深度 2）
            c.register_schema("A", "1", obj({"f": s("integer")}))
            c.register_schema("A", "2", obj({"f": s("number")}))
            for name, target in [("B", "A"), ("C", "B"), ("D", "C")]:
                c.register_schema(
                    name, "1",
                    obj({"ref": {"$ref": f"catalog://{target}@1#/"}}),
                )
            with self.assertRaises(ImpactAnalysisTooLarge):
                c.compare_schemas("A", "1", "2")
        finally:
            LIMITS["max_impact_depth"] = original


if __name__ == "__main__":
    unittest.main()
