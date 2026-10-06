import unittest

from meta_catalog import MetaCatalog, SearchQueryInvalid


def schema(**props):
    return {"type": "object", "properties": props}


def build_catalog():
    """Address(street,zip) <- Person(address->Address) 跨 Schema 引用，
    两个资产分别以直接 / 传递方式引用 Address 字段。"""
    c = MetaCatalog()
    c.register_schema(
        "Address", "1.0",
        schema(street={"type": "string"}, zip={"type": "string"}),
    )
    c.register_schema(
        "Person", "1.0",
        schema(name={"type": "string"}, address={"$ref": "Address@1.0#"}),
    )
    # svc-mail 直接引用 Address；svc-both 同时直接与传递引用 /street；
    # svc-billing 经 Person 传递引用 street 与 zip（同篇对 Person 去重）。
    c.register_asset(
        "svc-mail", "邮寄服务", "service",
        [{"schema": "Address", "version": "1.0", "path": "/street"}],
    )
    c.register_asset(
        "svc-billing", "账单服务", "service",
        [
            {"schema": "Person", "version": "1.0", "path": "/address/street"},
            {"schema": "Person", "version": "1.0", "path": "/address/zip"},
        ],
    )
    c.register_asset(
        "svc-both", "双全服务", "service",
        [
            {"schema": "Address", "version": "1.0", "path": "/street"},
            {"schema": "Person", "version": "1.0", "path": "/address/street"},
        ],
    )
    v2 = schema(street={"type": "integer"}, zip={"type": "integer"})
    c.compare_schemas("Address", "1.0", v2, candidate_version="2.0")
    return c


class SearchFacetsShapeTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()
        self.f = self.c.search_facets()

    def test_top_level_shape(self):
        self.assertEqual(
            set(self.f),
            {"total", "doc_type", "schema", "version", "change_kind",
             "compatibility", "asset"},
        )
        # 2 Schema + 3 资产 + 2 条字段变更
        self.assertEqual(self.f["total"], 7)

    def test_entry_shapes(self):
        for facet in ("doc_type", "schema", "version",
                      "change_kind", "compatibility"):
            for entry in self.f[facet]:
                self.assertEqual(set(entry), {"value", "count"}, facet)
                self.assertIsInstance(entry["count"], int)
                self.assertGreater(entry["count"], 0)
        for entry in self.f["asset"]:
            self.assertEqual(set(entry), {"asset_id", "name", "count"})
            self.assertGreater(entry["count"], 0)

    def test_empty_catalog(self):
        f = MetaCatalog().search_facets()
        self.assertEqual(f, {
            "total": 0,
            "doc_type": [],
            "schema": [],
            "version": [],
            "change_kind": [],
            "compatibility": [],
            "asset": [],
        })

    def test_partial_doc_types_are_valid(self):
        c = MetaCatalog()
        c.register_schema("Only", "1.0", schema(a={"type": "string"}))
        f = c.search_facets()
        self.assertEqual(f["total"], 1)
        self.assertEqual(f["doc_type"], [{"value": "schema", "count": 1}])
        self.assertEqual(f["schema"], [{"value": "Only", "count": 1}])
        self.assertEqual(f["version"], [{"value": "Only@1.0", "count": 1}])
        self.assertEqual(f["change_kind"], [])
        self.assertEqual(f["compatibility"], [])
        self.assertEqual(f["asset"], [])

        c.register_asset("a1", "资产一", "service", [])
        f = c.search_facets(doc_type="asset")
        self.assertEqual(f["total"], 1)
        self.assertEqual(f["schema"], [])
        self.assertEqual(f["version"], [])
        self.assertEqual(f["asset"], [
            {"asset_id": "a1", "name": "资产一", "count": 1}
        ])

    def test_no_keyword_hit(self):
        f = self.c.search_facets("绝对不存在的关键词zzz")
        self.assertEqual(f["total"], 0)
        self.assertEqual(f["doc_type"], [])
        self.assertEqual(f["asset"], [])


class SearchFacetsCountingTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_doc_type_counts_each_doc_once(self):
        f = self.c.search_facets()
        self.assertEqual(f["doc_type"], [
            {"value": "asset", "count": 3},
            {"value": "change", "count": 2},
            {"value": "schema", "count": 2},
        ])
        # 分面计数之和等于 total
        self.assertEqual(
            sum(e["count"] for e in f["doc_type"]), f["total"]
        )

    def test_schema_facet_sources_and_per_doc_dedup(self):
        f = self.c.search_facets()
        # Address：Schema 1 + 直接引用资产 svc-mail/svc-both 各 1
        #          + 两条变更报告各 1 = 5
        # Person ：Schema 1 + svc-billing、svc-both 各 1（同篇两引用只计一次）= 3
        self.assertEqual(f["schema"], [
            {"value": "Address", "count": 5},
            {"value": "Person", "count": 3},
        ])

    def test_version_facet_name_at_version_and_dedup(self):
        f = self.c.search_facets()
        # Address@1.0：Schema1 + svc-mail1 + svc-both1 + 两条变更基线 2 = 5
        # Person@1.0 ：Schema1 + svc-billing1 + svc-both1 = 3
        # Address@2.0 ：两条变更的候选版本 = 2
        self.assertEqual(f["version"], [
            {"value": "Address@1.0", "count": 5},
            {"value": "Person@1.0", "count": 3},
            {"value": "Address@2.0", "count": 2},
        ])

    def test_change_kind_and_compatibility_only_count_changes(self):
        f = self.c.search_facets()
        self.assertEqual(f["change_kind"], [
            {"value": "modified", "count": 2}
        ])
        self.assertEqual(f["compatibility"], [
            {"value": "breaking", "count": 2}
        ])

    def test_asset_facet_union_direct_and_transitive_once_per_doc(self):
        f = self.c.search_facets()
        by_id = {e["asset_id"]: e for e in f["asset"]}
        # /street 变更：svc-mail 直接、svc-billing 传递、svc-both 直接∪传递
        # /zip   变更：仅 svc-billing 传递
        # 外加各资产自身的命中文档各 1
        self.assertEqual(by_id["svc-billing"], {
            "asset_id": "svc-billing", "name": "账单服务", "count": 3
        })
        self.assertEqual(by_id["svc-mail"], {
            "asset_id": "svc-mail", "name": "邮寄服务", "count": 2
        })
        # svc-both 同时是直接与传递影响资产，并集中只计一次
        self.assertEqual(by_id["svc-both"], {
            "asset_id": "svc-both", "name": "双全服务", "count": 2
        })

    def test_facets_follow_filtered_hit_set(self):
        f = self.c.search_facets(field_path="/zip")
        self.assertEqual(f["total"], 1)
        self.assertEqual(f["doc_type"], [{"value": "change", "count": 1}])
        self.assertEqual(f["schema"], [{"value": "Address", "count": 1}])
        self.assertEqual(f["version"], [
            {"value": "Address@1.0", "count": 1},
            {"value": "Address@2.0", "count": 1},
        ])
        self.assertEqual(f["asset"], [
            {"asset_id": "svc-billing", "name": "账单服务", "count": 1}
        ])

    def test_schema_filter_excludes_other_types_consistently(self):
        f = self.c.search_facets(schema="Person")
        # Person Schema 文档 + 引用 Person 的两个资产（变更报告都是 Address）。
        # svc-both 同时引用 Address，命中后其全部引用 Schema 仍计入分面。
        self.assertEqual(f["total"], 3)
        self.assertEqual(f["schema"], [
            {"value": "Person", "count": 3},
            {"value": "Address", "count": 1},
        ])
        self.assertEqual(f["version"], [
            {"value": "Person@1.0", "count": 3},
            {"value": "Address@1.0", "count": 1},
        ])
        self.assertEqual(f["change_kind"], [])
        self.assertEqual(f["compatibility"], [])
        ids = {e["asset_id"] for e in f["asset"]}
        self.assertEqual(ids, {"svc-billing", "svc-both"})

    def test_compatibility_filter_only_changes(self):
        f = self.c.search_facets(compatibility="breaking")
        self.assertEqual(f["total"], 2)
        self.assertEqual(f["doc_type"], [{"value": "change", "count": 2}])
        self.assertEqual(f["asset"], [
            {"asset_id": "svc-billing", "name": "账单服务", "count": 2},
            {"asset_id": "svc-both", "name": "双全服务", "count": 1},
            {"asset_id": "svc-mail", "name": "邮寄服务", "count": 1},
        ])

    def test_change_kind_filter_miss(self):
        f = self.c.search_facets(change_kind="added")
        self.assertEqual(f["total"], 0)
        self.assertEqual(f["asset"], [])

    def test_asset_name_filter(self):
        f = self.c.search_facets(asset_name="账单")
        # 命中：svc-billing 资产自身 + 两条影响它的变更（共 3 篇）。
        self.assertEqual(f["total"], 3)
        # 资产分面仍按每篇命中的完整影响并集聚合：street 变更同时影响
        # svc-mail / svc-both，故它们各计 1；svc-billing 在三篇中各计 1。
        self.assertEqual(f["asset"], [
            {"asset_id": "svc-billing", "name": "账单服务", "count": 3},
            {"asset_id": "svc-both", "name": "双全服务", "count": 1},
            {"asset_id": "svc-mail", "name": "邮寄服务", "count": 1},
        ])

    def test_doc_type_filter_asset_has_no_change_facets(self):
        f = self.c.search_facets(doc_type="asset")
        self.assertEqual(f["total"], 3)
        self.assertEqual(
            {e["value"] for e in f["doc_type"]}, {"asset"}
        )
        self.assertEqual(f["change_kind"], [])
        self.assertEqual(f["compatibility"], [])


class SearchFacetsSameHitSetTests(unittest.TestCase):
    """分面必须与 search 同条件、同 AND 组合、同完整命中集合（不受 limit 截断）。"""

    def setUp(self):
        self.c = build_catalog()

    def assertTotalMatchesSearch(self, *args, **kwargs):
        hits = self.c.search(*args, **kwargs)
        facets = self.c.search_facets(*args, **kwargs)
        self.assertEqual(facets["total"], len(hits), (args, kwargs))
        return hits, facets

    def test_total_equals_search_for_queries(self):
        for args, kwargs in [
            ((), {}),
            (("street",), {}),
            (("Address",), {}),
            ((), {"doc_type": "change"}),
            ((), {"schema": "Address"}),
            ((), {"version": "Address@2.0"}),
            ((), {"change_kind": "modified"}),
            ((), {"compatibility": "breaking"}),
            ((), {"field_path": "/street"}),
            ((), {"asset_name": "服务"}),
            (("street",), {"doc_type": "change"}),
            (("Address",), {"schema": "Address"}),
            (("不存在",), {}),
        ]:
            self.assertTotalMatchesSearch(*args, **kwargs)

    def test_doc_type_partition_sums_to_total(self):
        for kwargs in ({}, {"schema": "Address"}, {"keyword": "street"}):
            f = self.c.search_facets(**kwargs)
            self.assertEqual(
                sum(e["count"] for e in f["doc_type"]), f["total"]
            )

    def test_not_truncated_by_any_limit(self):
        # search 接受 limit 截断；facets 不接受 limit，且始终聚合完整命中。
        truncated = self.c.search(limit=1)
        self.assertEqual(len(truncated), 1)
        f = self.c.search_facets()
        self.assertEqual(f["total"], 7)


class SearchFacetsSortTests(unittest.TestCase):
    def test_value_facets_count_desc_value_asc(self):
        c = MetaCatalog()
        c.register_schema("S00", "1.0", schema(a={}))
        c.register_schema("S01", "1.0", schema(a={}))
        c.register_schema("S02", "1.0", schema(a={}))
        # 一个资产引用三个 Schema：各 Schema 计数并列，按 value 升序
        c.register_asset(
            "a0", "引用者", "service",
            [{"schema": s, "version": "1.0", "path": "/a"}
             for s in ("S00", "S01", "S02")],
        )
        # 再给 S02 加一次引用制造计数差
        c.register_asset(
            "a1", "另一个", "service",
            [{"schema": "S02", "version": "1.0", "path": "/a"}],
        )
        f = c.search_facets()
        self.assertEqual(f["schema"], [
            {"value": "S02", "count": 3},  # Schema1 + 两资产
            {"value": "S00", "count": 2},
            {"value": "S01", "count": 2},
        ])

    def test_asset_facet_count_desc_name_asc(self):
        c = MetaCatalog()
        # 并列时按 name 而非 asset_id：id 顺序与名称顺序故意相反
        c.register_asset("zzz", "B资产", "service", [])
        c.register_asset("aaa", "A资产", "service", [])
        f = c.search_facets()
        self.assertEqual(
            [(e["name"], e["count"]) for e in f["asset"]],
            [("A资产", 1), ("B资产", 1)],
        )

        c.register_asset("mmm", "C资产", "service", [])
        c.register_asset("nnn", "A二", "service", [])
        f = c.search_facets()
        # 当前各资产均只被自身文档命中一次，全部按 name 升序
        self.assertEqual(
            [e["name"] for e in f["asset"]],
            ["A二", "A资产", "B资产", "C资产"],
        )


class SearchFacetsCandidateLabelTests(unittest.TestCase):
    def test_missing_candidate_version_only_counts_baseline(self):
        c = MetaCatalog()
        c.register_schema("Person", "1.0", schema(name={"type": "string"}))
        # 内联候选、不带 candidate_version：报告候选版本为 None
        c.compare_schemas("Person", "1.0", schema(name={"type": "integer"}))
        f = c.search_facets(doc_type="change")
        self.assertEqual(f["total"], 1)
        self.assertEqual(f["version"], [
            {"value": "Person@1.0", "count": 1}
        ])
        self.assertEqual(f["schema"], [
            {"value": "Person", "count": 1}
        ])


class SearchFacetsValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_non_string_keyword_rejected(self):
        for bad in (1, 1.5, ["x"], {"x": 1}, True, False):
            with self.assertRaises(SearchQueryInvalid, msg=repr(bad)):
                self.c.search_facets(bad)

    def test_non_string_filter_rejected(self):
        for name in ("doc_type", "schema", "version", "field_path",
                     "change_kind", "compatibility", "asset_name"):
            with self.assertRaises(SearchQueryInvalid, msg=name):
                self.c.search_facets(**{name: 123})
            with self.assertRaises(SearchQueryInvalid, msg=name):
                self.c.search_facets(**{name: True})

    def test_pagination_and_unknown_kwargs_rejected(self):
        for kwargs in (
            {"limit": 3},
            {"page_size": 10},
            {"cursor": "x"},
            {"offset": 0},
            {"limit": None},
            {"unknown_thing": "x"},
        ):
            with self.assertRaises(SearchQueryInvalid, msg=str(kwargs)):
                self.c.search_facets(**kwargs)

    def test_error_code(self):
        try:
            self.c.search_facets(limit=1)
        except SearchQueryInvalid as exc:
            self.assertEqual(exc.code, "SearchQueryInvalid")
        else:
            self.fail("应当抛 SearchQueryInvalid")


class SearchFacetsReadOnlyTests(unittest.TestCase):
    def test_repeated_calls_are_stable_and_readonly(self):
        c = build_catalog()
        before_search = c.search()
        before_reports = c.list_reports()
        before_versions = c.list_versions("Address")
        f1 = c.search_facets()
        f2 = c.search_facets("street")
        f3 = c.search_facets(schema="Address")
        f4 = c.search_facets()
        self.assertEqual(f1, f4)
        self.assertEqual(c.search(), before_search)
        self.assertEqual(c.list_reports(), before_reports)
        self.assertEqual(c.list_versions("Address"), before_versions)
        # 返回值不共享可变状态：改动返回结构不影响后续调用
        f1["asset"].clear()
        f1["total"] = -1
        self.assertEqual(c.search_facets(), f4)
        self.assertIsNot(f2, f3)

    def test_facets_then_register_and_compare_unchanged_semantics(self):
        c = build_catalog()
        c.search_facets()
        before = c.search_facets()
        before_schema_address_total = c.search_facets(schema="Address")["total"]
        # 后续注册与比较正常生效，分面以调用时索引为准
        c.register_schema("NewOne", "1.0", schema(q={"type": "string"}))
        after_register = c.search_facets()
        self.assertEqual(after_register["total"], before["total"] + 1)
        self.assertIn({"value": "NewOne", "count": 1}, after_register["schema"])
        # 旧报告与检索行为不变
        self.assertEqual(
            c.search_facets(schema="Address")["total"],
            before_schema_address_total,
        )
        reports_before = len(c.list_reports())
        c.compare_schemas(
            "NewOne", "1.0",
            schema(q={"type": "integer"}), candidate_version="2.0",
        )
        self.assertEqual(len(c.list_reports()), reports_before + 1)
        f = c.search_facets(schema="NewOne", doc_type="change")
        self.assertEqual(f["total"], 1)

    def test_empty_catalog_queries_never_raise(self):
        c = MetaCatalog()
        self.assertEqual(c.search_facets()["total"], 0)
        self.assertEqual(c.search_facets("任意词")["total"], 0)
        self.assertEqual(c.search_facets(doc_type="change")["total"], 0)
        self.assertEqual(c.search_facets(schema="X", version="X@1")["total"], 0)


if __name__ == "__main__":
    unittest.main()
