import unittest

from meta_catalog import MetaCatalog, CatalogError, SearchQueryInvalid
from meta_catalog.errors import SearchQueryInvalid as ErrorsSearchQueryInvalid


def build_catalog():
    """两个 Schema、两个资产、一次 Person 1.0 -> 2.0 的比较。"""
    c = MetaCatalog()
    person_v1 = {
        "type": "object",
        "title": "Person schema",
        "properties": {
            "name": {"type": "string", "title": "姓名"},
            "age": {"type": "integer"},
            "level": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["name"],
    }
    c.register_schema("Person", "1.0", person_v1)
    c.register_schema(
        "Order", "1.0",
        {"type": "object", "title": "Order schema",
         "properties": {"id": {"type": "string"}}},
    )
    c.register_asset(
        "svc-mail", "邮寄服务", "service",
        [{"schema": "Person", "version": "1.0", "path": "/name"}],
    )
    c.register_asset(
        "svc-order", "订单服务", "service",
        [{"schema": "Order", "version": "1.0", "path": "/id"},
         {"schema": "Person", "version": "1.0", "path": "/age"}],
    )
    person_v2 = {
        "type": "object",
        "title": "Person schema",
        "properties": {
            "name": {"type": "string", "title": "姓名", "description": "全名"},
            "age": {"type": "number"},
            "level": {"type": "string", "enum": ["a"]},
        },
        "required": ["name", "age"],
    }
    c.compare_schemas("Person", "1.0", person_v2, candidate_version="2.0")
    return c


def expected_facets(hits):
    """按规格从 search 命中集合独立计算分面。"""
    doc_type, schema, version = {}, {}, {}
    change_kind, compatibility = {}, {}
    assets = {}
    for h in hits:
        t = h["type"]
        doc_type[t] = doc_type.get(t, 0) + 1
        if t == "schema":
            schemas = {h["name"]}
            versions = {f"{h['name']}@{h['version']}"}
            asset_map = {}
        elif t == "asset":
            schemas = set(h["schemas"])
            versions = set(h["versions"])
            asset_map = {h["id"]: h["name"]}
        else:
            schemas = {h["schema"]}
            versions = {
                f"{h['schema']}@{v}"
                for v in (h["baseline_version"], h["candidate_version"])
                if v
            }
            asset_map = {a["asset_id"]: a["name"] for a in h["matched_assets"]}
            k = h["change_kind"]
            change_kind[k] = change_kind.get(k, 0) + 1
            comp = h["compatibility"]
            compatibility[comp] = compatibility.get(comp, 0) + 1
        for s in schemas:
            schema[s] = schema.get(s, 0) + 1
        for v in versions:
            version[v] = version.get(v, 0) + 1
        for aid, nm in asset_map.items():
            entry = assets.setdefault(aid, [nm, 0])
            entry[1] += 1

    def value_facets(counts):
        return [
            {"value": v, "count": n}
            for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    return {
        "total": len(hits),
        "doc_type": value_facets(doc_type),
        "schema": value_facets(schema),
        "version": value_facets(version),
        "change_kind": value_facets(change_kind),
        "compatibility": value_facets(compatibility),
        "asset": [
            {"asset_id": aid, "name": nm, "count": n}
            for aid, (nm, n) in sorted(assets.items(), key=lambda kv: (-kv[1][1], kv[0]))
        ],
    }


class SearchFacetsBasicTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_result_shape(self):
        facets = self.c.search_facets()
        self.assertEqual(
            set(facets),
            {"total", "doc_type", "schema", "version",
             "change_kind", "compatibility", "asset"},
        )
        self.assertGreater(facets["total"], 0)
        for entry in facets["asset"]:
            self.assertEqual(set(entry), {"asset_id", "name", "count"})
        for name in ("doc_type", "schema", "version", "change_kind", "compatibility"):
            for entry in facets[name]:
                self.assertEqual(set(entry), {"value", "count"})

    def test_empty_catalog(self):
        facets = MetaCatalog().search_facets()
        self.assertEqual(facets["total"], 0)
        for name in ("doc_type", "schema", "version",
                     "change_kind", "compatibility", "asset"):
            self.assertEqual(facets[name], [])

    def test_keyword_no_hits(self):
        facets = self.c.search_facets("不存在的关键词xyz")
        self.assertEqual(facets["total"], 0)
        for name in ("doc_type", "schema", "version",
                     "change_kind", "compatibility", "asset"):
            self.assertEqual(facets[name], [])

    def test_matches_full_hit_set_across_queries(self):
        queries = [
            ({}, {}),
            (("name",), {}),
            (("schema",), {"doc_type": "schema"}),
            ((), {"doc_type": "asset"}),
            ((), {"doc_type": "change"}),
            ((), {"schema": "Person"}),
            ((), {"version": "1.0"}),
            ((), {"change_kind": "modified"}),
            ((), {"compatibility": "breaking"}),
            ((), {"asset_name": "邮寄"}),
            (("Person",), {"doc_type": "change", "compatibility": "breaking"}),
        ]
        for args, filters in queries:
            keyword = args[0] if args else None
            with self.subTest(keyword=keyword, filters=filters):
                hits = self.c.search(keyword, **filters)
                self.assertEqual(
                    self.c.search_facets(keyword, **filters),
                    expected_facets(hits),
                )

    def test_aggregation_not_truncated_by_limit(self):
        limited = self.c.search(limit=1)
        self.assertEqual(len(limited), 1)
        facets = self.c.search_facets()
        self.assertEqual(facets["total"], len(self.c.search()))
        self.assertGreater(facets["total"], 1)

    def test_doc_type_counts(self):
        facets = self.c.search_facets()
        counts = {e["value"]: e["count"] for e in facets["doc_type"]}
        self.assertEqual(counts["schema"], 2)
        self.assertEqual(counts["asset"], 2)
        self.assertEqual(counts["change"], len(self.c.search(doc_type="change")))

    def test_schema_facet_values(self):
        facets = self.c.search_facets()
        counts = {e["value"]: e["count"] for e in facets["schema"]}
        # Person：Schema 文档 + svc-mail/svc-order 引用 + 变更报告
        self.assertEqual(
            counts["Person"],
            1 + 2 + len(self.c.search(doc_type="change")),
        )
        # Order：Schema 文档 + svc-order 引用
        self.assertEqual(counts["Order"], 2)

    def test_version_facet_uses_name_at_version(self):
        facets = self.c.search_facets()
        values = {e["value"] for e in facets["version"]}
        self.assertIn("Person@1.0", values)
        self.assertIn("Order@1.0", values)
        # 变更报告计基线与候选版本
        self.assertIn("Person@2.0", values)
        for value in values:
            self.assertIn("@", value)

    def test_change_without_candidate_version_label(self):
        c = MetaCatalog()
        c.register_schema(
            "S", "1.0",
            {"type": "object", "properties": {"a": {"type": "string"}}},
        )
        c.compare_schemas(
            "S", "1.0",
            {"type": "object", "properties": {"a": {"type": "integer"}}},
        )
        facets = c.search_facets(doc_type="change")
        self.assertGreater(facets["total"], 0)
        # 候选无版本标签：变更只计基线版本
        self.assertEqual(facets["version"], [{"value": "S@1.0", "count": facets["total"]}])

    def test_change_kind_and_compatibility_only_from_changes(self):
        facets = self.c.search_facets(doc_type="schema")
        self.assertGreater(facets["total"], 0)
        self.assertEqual(facets["change_kind"], [])
        self.assertEqual(facets["compatibility"], [])
        self.assertEqual(facets["asset"], [])

    def test_asset_facet_counts_self_and_impacted(self):
        facets = self.c.search_facets()
        counts = {e["asset_id"]: e["count"] for e in facets["asset"]}
        impacted_by_changes = len(self.c.search(doc_type="change", asset_name="邮寄"))
        # svc-mail：资产自身 1 次 + 每条直接影响它的变更 1 次
        self.assertEqual(counts["svc-mail"], 1 + impacted_by_changes)
        # 资产条目按 asset_id 携带名称
        names = {e["asset_id"]: e["name"] for e in facets["asset"]}
        self.assertEqual(names["svc-mail"], "邮寄服务")

    def test_facets_sorted_by_count_desc_value_asc(self):
        facets = self.c.search_facets()
        for name in ("doc_type", "schema", "version", "change_kind", "compatibility"):
            entries = facets[name]
            keys = [(-e["count"], e["value"]) for e in entries]
            self.assertEqual(keys, sorted(keys), msg=name)
        asset_keys = [(-e["count"], e["asset_id"]) for e in facets["asset"]]
        self.assertEqual(asset_keys, sorted(asset_keys))

    def test_only_positive_counts_listed(self):
        facets = self.c.search_facets()
        for name in ("doc_type", "schema", "version",
                     "change_kind", "compatibility", "asset"):
            for entry in facets[name]:
                self.assertGreater(entry["count"], 0)


class SearchFacetsValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_error_type_and_code(self):
        self.assertTrue(issubclass(SearchQueryInvalid, CatalogError))
        self.assertIs(SearchQueryInvalid, ErrorsSearchQueryInvalid)
        with self.assertRaises(SearchQueryInvalid) as ctx:
            self.c.search_facets(limit=1)
        self.assertEqual(ctx.exception.code, "SearchQueryInvalid")

    def test_rejects_pagination_and_truncation_args(self):
        for kwargs in (
            {"limit": 1},
            {"page_size": 10},
            {"cursor": "mcsp1.x.y"},
            {"offset": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(SearchQueryInvalid):
                    self.c.search_facets(**kwargs)

    def test_rejects_unknown_argument(self):
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_facets(unknown_param="x")

    def test_rejects_non_string_values(self):
        for kwargs in (
            {"keyword": 123},
            {"keyword": ["a"]},
            {"doc_type": 1},
            {"schema": b"Person"},
            {"version": 1.0},
            {"field_path": {}},
            {"change_kind": True},
            {"compatibility": 0},
            {"asset_name": ("邮寄",)},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(SearchQueryInvalid):
                    self.c.search_facets(**kwargs)

    def test_none_values_are_accepted(self):
        facets = self.c.search_facets(
            None, doc_type=None, schema=None, version=None, field_path=None,
            change_kind=None, compatibility=None, asset_name=None,
        )
        self.assertEqual(facets["total"], len(self.c.search()))


class SearchFacetsReadOnlyTests(unittest.TestCase):
    def test_repeated_calls_and_state_unchanged(self):
        c = build_catalog()
        before_search = c.search()
        before_reports = c.list_reports()

        first = c.search_facets()
        second = c.search_facets()
        self.assertEqual(first, second)

        # 连续聚合不改变检索结果、报告库；后续注册与比较行为不变
        self.assertEqual(c.search(), before_search)
        self.assertEqual(c.list_reports(), before_reports)

        c.register_schema(
            "Extra", "1.0", {"type": "object", "properties": {}},
        )
        self.assertEqual(c.search_facets()["total"], first["total"] + 1)
        c.compare_schemas(
            "Extra", "1.0",
            {"type": "object", "properties": {"x": {"type": "string"}}},
            candidate_version="2.0",
        )
        after = c.search_facets()
        self.assertGreater(after["total"], first["total"] + 1)
        change_kinds = {e["value"] for e in after["change_kind"]}
        self.assertIn("added", change_kinds)


if __name__ == "__main__":
    unittest.main()
