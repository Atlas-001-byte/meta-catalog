"""search_page 分页入口的测试。

覆盖：与 search 逐项同构、连续翻页不重复不遗漏、空结果与末页、参数与游标
非法口径、分页期间索引变化的行为，以及只读 / 既有行为不改变。
"""

import copy
import itertools
import unittest

from meta_catalog import CatalogError, MetaCatalog, SearchQueryInvalid


def item_key(hit):
    """search 结果的稳定身份键：排序分以外，用类型 + 确定性定位字段。"""
    return (
        hit["type"],
        hit.get("name")
        or hit.get("id")
        or f"{hit.get('report_id')}:{hit.get('path')}",
    )


def build_catalog(n_schemas=6, n_assets=5):
    c = MetaCatalog()
    for i in range(n_schemas):
        c.register_schema(
            f"Schema{i}",
            "1.0",
            {"type": "object", "title": f"page schema {i}",
             "properties": {f"f{i}": {"type": "string"}}},
        )
    for i in range(n_assets):
        c.register_asset(
            f"asset-{i}", f"资产{i}", "service",
            [{"schema": f"Schema{i % n_schemas}", "version": "1.0",
              "path": f"/f{i % n_schemas}"}],
        )
    return c


def walk_pages(c, **kwargs):
    """连续翻页，返回 (所有 items, 每一页原始返回)。"""
    pages = []
    cursor = None
    while True:
        page = c.search_page(cursor=cursor, **kwargs) if cursor is not None \
            else c.search_page(**kwargs)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    return list(itertools.chain.from_iterable(p["items"] for p in pages)), pages


class SearchPageBasicTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_first_page_shape_defaults(self):
        page = self.c.search_page()
        self.assertEqual(set(page), {"items", "total", "page_size", "next_cursor"})
        self.assertEqual(page["page_size"], 50)
        # 11 个文档 < 默认 page_size -> 单页，末页游标为 None
        self.assertEqual(page["total"], 11)
        self.assertEqual(len(page["items"]), 11)
        self.assertIsNone(page["next_cursor"])

    def test_items_identical_to_search(self):
        for kwargs in (
            {},
            {"keyword": "schema"},
            {"doc_type": "asset"},
            {"keyword": "资产", "doc_type": "asset"},
            {"schema": "Schema0", "page_size": 2},
        ):
            pk = {k: v for k, v in kwargs.items() if k != "page_size"}
            full = self.c.search(**pk)
            paged, _ = walk_pages(self.c, **kwargs)
            self.assertEqual(len(paged), len(full), kwargs)
            self.assertEqual(paged, full, kwargs)

    def test_total_is_current_hit_count(self):
        page = self.c.search_page(doc_type="schema", page_size=3)
        self.assertEqual(page["total"], 6)
        self.assertEqual(len(page["items"]), 3)
        keyworded = self.c.search_page("schema", page_size=100)
        self.assertEqual(keyworded["total"], len(self.c.search("schema")))

    def test_paging_covers_every_hit_once(self):
        for size in (1, 2, 3, 5, 11, 50):
            items, pages = walk_pages(self.c, page_size=size)
            keys = [item_key(h) for h in items]
            self.assertEqual(len(keys), len(set(keys)), f"size={size} 有重复")
            self.assertEqual(len(keys), self.c.search().__len__(), f"size={size} 有遗漏")
            for p in pages[:-1]:
                self.assertEqual(len(p["items"]), size)
            self.assertLessEqual(len(pages[-1]["items"]), size)
            self.assertIsNone(pages[-1]["next_cursor"])

    def test_page_sizes_exact_multiple_last_page_empty_cursor(self):
        # total=11，page_size=11 恰好一页
        page = self.c.search_page(page_size=11)
        self.assertEqual(len(page["items"]), 11)
        self.assertIsNone(page["next_cursor"])
        # page_size=1 -> 11 页，最后一页 1 条
        items, pages = walk_pages(self.c, page_size=1)
        self.assertEqual(len(pages), 11)
        self.assertEqual(len(pages[-1]["items"]), 1)

    def test_empty_result_first_page(self):
        page = self.c.search_page("不存在的关键词xyz")
        self.assertEqual(page["items"], [])
        self.assertEqual(page["total"], 0)
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(page["page_size"], 50)

    def test_empty_result_with_filters(self):
        page = self.c.search_page(doc_type="change", page_size=10)
        self.assertEqual((page["items"], page["total"], page["next_cursor"]), ([], 0, None))

    def test_each_page_total_reflects_query(self):
        _, pages = walk_pages(self.c, doc_type="schema", page_size=2)
        self.assertEqual([p["total"] for p in pages], [6, 6, 6])


class SearchPageCursorTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_cursor_is_opaque_string(self):
        page = self.c.search_page(page_size=2)
        cur = page["next_cursor"]
        self.assertIsInstance(cur, str)
        self.assertTrue(cur)
        # 不应暴露内部偏移明文
        self.assertNotIn("page_size", cur)

    def test_cursor_bound_to_query_conditions(self):
        first = self.c.search_page("schema", page_size=2)
        cur = first["next_cursor"]
        # keyword 变化
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page("asset", page_size=2, cursor=cur)
        # 任一结构化过滤变化
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page("schema", doc_type="schema", page_size=2, cursor=cur)
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page("schema", schema="Schema0", page_size=2, cursor=cur)
        # page_size 变化
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page("schema", page_size=3, cursor=cur)
        # 原查询仍可正常续页
        second = self.c.search_page("schema", page_size=2, cursor=cur)
        self.assertEqual(len(second["items"]), 2)

    def test_cursor_not_reusable_across_instances(self):
        other = build_catalog()
        cur = self.c.search_page(page_size=2)["next_cursor"]
        with self.assertRaises(SearchQueryInvalid):
            other.search_page(page_size=2, cursor=cur)

    def test_cursor_bound_to_all_filters_individually(self):
        base = {"schema": "Schema0", "page_size": 1}
        cur = self.c.search_page(**base)["next_cursor"]
        for name, value in (
            ("version", "1.0"),
            ("field_path", "/f0"),
            ("change_kind", "added"),
            ("compatibility", "breaking"),
            ("asset_name", "资产"),
            ("doc_type", "schema"),
        ):
            with self.assertRaises(SearchQueryInvalid):
                params = dict(base)
                params[name] = value
                self.c.search_page(cursor=cur, **params)

    def test_malformed_cursors_rejected(self):
        valid = self.c.search_page(page_size=2)["next_cursor"]
        payload, sig = valid.split(".")
        bad_cursors = [
            "",
            ".",
            "..",
            "nodot",
            f"{payload}.",
            f".{sig}",
            f"{payload}.{sig[:-1]}",  # 截断签名
            valid + "x",
            "!!!.@@@",
            "a.b.c",
        ]
        for bad in bad_cursors:
            with self.assertRaises(SearchQueryInvalid, msg=bad):
                self.c.search_page(page_size=2, cursor=bad)
        # 篡改载荷（重签失败）
        tampered = ("AAAA" if payload[:4] != "AAAA" else "BBBB") + payload[4:]
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page(page_size=2, cursor=f"{tampered}.{sig}")

    def test_non_string_cursor_rejected(self):
        for bad in (123, 12.0, True, b"x", [], {}):
            with self.assertRaises(SearchQueryInvalid, msg=repr(bad)):
                self.c.search_page(page_size=2, cursor=bad)

    def test_intermediate_cursor_reusable_same_query_only(self):
        # 中间页游标在同查询内可用；换 page_size 后不可复用。
        page = self.c.search_page(page_size=5)  # total=11
        cur = page["next_cursor"]
        self.assertIsNotNone(cur)
        again = self.c.search_page(page_size=5, cursor=cur)
        self.assertEqual(len(again["items"]), 5)
        self.assertIsNotNone(again["next_cursor"])
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page(page_size=6, cursor=cur)


class SearchPageValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_invalid_page_size(self):
        for bad in (0, -1, 201, 1000, True, False, 1.0, "5", None, [5]):
            with self.assertRaises(SearchQueryInvalid, msg=repr(bad)):
                self.c.search_page(page_size=bad)

    def test_page_size_boundaries_accepted(self):
        self.assertEqual(self.c.search_page(page_size=1)["page_size"], 1)
        page = self.c.search_page(page_size=200)
        self.assertEqual(page["page_size"], 200)
        self.assertIsNone(page["next_cursor"])

    def test_invalid_keyword_and_filters(self):
        for kwargs in (
            {"keyword": 123},
            {"keyword": True},
            {"keyword": ["a"]},
            {"schema": 1},
            {"version": 2.0},
            {"field_path": True},
            {"change_kind": b"x"},
            {"compatibility": []},
            {"asset_name": {}},
            {"doc_type": 5},
        ):
            with self.assertRaises(SearchQueryInvalid, msg=repr(kwargs)):
                self.c.search_page(**kwargs)

    def test_limit_and_unknown_kwargs_rejected(self):
        for kwargs in (
            {"limit": 10},
            {"offset": 0},
            {"foo": "bar"},
            {"page_size": 2, "limit": 2},
        ):
            with self.assertRaises(SearchQueryInvalid, msg=repr(kwargs)):
                self.c.search_page(**kwargs)

    def test_error_is_catalog_error_with_fixed_code(self):
        self.assertTrue(issubclass(SearchQueryInvalid, CatalogError))
        self.assertEqual(SearchQueryInvalid.code, "SearchQueryInvalid")
        try:
            self.c.search_page(page_size=0)
        except SearchQueryInvalid as exc:
            self.assertEqual(exc.code, "SearchQueryInvalid")
            self.assertIsInstance(exc, CatalogError)
        else:
            self.fail("应当抛 SearchQueryInvalid")

    def test_validation_failure_does_not_mutate(self):
        before = self.c.search()
        for bad_kwargs in ({"page_size": 0}, {"limit": 1}, {"keyword": 1}):
            with self.assertRaises(SearchQueryInvalid):
                self.c.search_page(**bad_kwargs)
        self.assertEqual(self.c.search(), before)


class SearchPageChangeTests(unittest.TestCase):
    def test_new_docs_during_paging(self):
        c = build_catalog(n_schemas=4, n_assets=0)  # 4 个 schema 文档
        first = c.search_page(doc_type="schema", page_size=2)
        self.assertEqual(first["total"], 4)
        first_keys = [item_key(h) for h in first["items"]]

        # 分页期间注册新 Schema；确定性键 ("schema", name, version) 排序插入。
        c.register_schema("SchemaZ", "1.0", {"type": "object", "title": "late z"})

        # 已完成页不改变；SchemaZ 键排在 Schema0..3 之后，只进入后续页，
        # 旧项不重复；续页与 total 均以调用时索引为准。
        rest_pages = []
        cur = first["next_cursor"]
        while cur is not None:
            p = c.search_page(doc_type="schema", page_size=2, cursor=cur)
            rest_pages.append(p)
            cur = p["next_cursor"]
        self.assertEqual([p["total"] for p in rest_pages], [5, 5])
        all_keys = first_keys + [
            item_key(h) for p in rest_pages for h in p["items"]
        ]
        self.assertEqual(len(all_keys), len(set(all_keys)))
        self.assertEqual(
            set(all_keys),
            {("schema", f"Schema{i}") for i in range(4)} | {("schema", "SchemaZ")},
        )
        self.assertIn(
            ("schema", "SchemaZ"),
            [item_key(h) for h in rest_pages[-1]["items"]],
        )

    def test_total_changes_with_index_on_each_call(self):
        c = build_catalog(n_schemas=3, n_assets=0)
        page = c.search_page(page_size=2)
        self.assertEqual(page["total"], 3)
        cur = page["next_cursor"]
        c.register_schema("Extra", "1.0", {"type": "object"})
        nxt = c.search_page(page_size=2, cursor=cur)
        self.assertEqual(nxt["total"], 4)

    def test_new_doc_before_continuation_position(self):
        # 新文档确定性键排在续页位置之前：规格只承诺“键排在续页位置后的新文档
        # 进入后续页”，早期键新文档不进入后续页（已完成页不回溯）。
        c = build_catalog(n_schemas=4, n_assets=0)
        first = c.search_page(doc_type="schema", page_size=2)
        c.register_schema("AAA", "1.0", {"type": "object"})  # 键排在最前
        second = c.search_page(
            doc_type="schema", page_size=2, cursor=first["next_cursor"]
        )
        second_keys = {item_key(h) for h in second["items"]}
        self.assertNotIn(("schema", "AAA"), second_keys)
        self.assertEqual(second["total"], 5)

    def test_report_added_during_paging_enters_later_pages(self):
        c = MetaCatalog()
        c.register_schema("P", "1.0", {"type": "object",
                                      "properties": {"a": {"type": "string"}}})
        # 先放一个资产文档：确定性键序为 asset < change < schema，
        # 保证续页位置（offset=1）落在新 change 文档之前。
        c.register_asset(
            "a-1", "资产一", "service",
            [{"schema": "P", "version": "1.0", "path": "/a"}],
        )
        first = c.search_page(page_size=1)
        self.assertEqual(first["total"], 2)
        seen = [item_key(h) for h in first["items"]]
        cur = first["next_cursor"]
        c.compare_schemas(
            "P", "1.0",
            {"type": "object",
             "properties": {"a": {"type": "integer"}}},
        )
        # 新 change 文档键排在续页位置之后，进入后续页。
        pages = []
        while cur is not None:
            p = c.search_page(page_size=1, cursor=cur)
            pages.append(p)
            cur = p["next_cursor"]
        all_keys = seen + [item_key(h) for p in pages for h in p["items"]]
        self.assertEqual(len(all_keys), len(set(all_keys)))
        self.assertTrue(any(k[0] == "change" for k in all_keys))


class SearchPageReadOnlyTests(unittest.TestCase):
    def test_search_page_is_read_only(self):
        c = build_catalog()
        before = {
            "search": copy.deepcopy(c.search()),
            "versions": c.list_versions("Schema0"),
            "reports": c.list_reports(),
        }
        walk_pages(c, page_size=2)
        c.search_page("schema", doc_type="schema", page_size=3)
        self.assertEqual(c.search(), before["search"])
        self.assertEqual(c.list_versions("Schema0"), before["versions"])
        self.assertEqual(c.list_reports(), before["reports"])

    def test_search_semantics_unchanged(self):
        c = build_catalog()
        # search 仍接受 limit，行为与不传 limit 一致（仅切片）。
        full = c.search("schema")
        self.assertEqual(c.search("schema", limit=2), full[:2])
        self.assertEqual(c.search("schema", limit=None), full)


if __name__ == "__main__":
    unittest.main()
