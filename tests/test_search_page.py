import unittest

from meta_catalog import MetaCatalog, CatalogError, SearchQueryInvalid
from meta_catalog.errors import SearchQueryInvalid as ErrorsSearchQueryInvalid


def build_catalog(n=7):
    c = MetaCatalog()
    for i in range(n):
        c.register_schema(
            f"S{i:02d}", "1.0",
            {"type": "object", "title": f"schema {i:02d}"},
        )
    return c


def page_keys(page):
    return [(h["type"], h.get("name") or h.get("path") or h.get("id")) for h in page["items"]]


class SearchPageBasicTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_first_page_shape(self):
        page = self.c.search_page(page_size=3)
        self.assertEqual(set(page), {"items", "total", "page_size", "next_cursor"})
        self.assertEqual(len(page["items"]), 3)
        self.assertEqual(page["total"], 7)
        self.assertEqual(page["page_size"], 3)
        self.assertIsInstance(page["next_cursor"], str)

    def test_default_page_size_is_50(self):
        page = self.c.search_page()
        self.assertEqual(page["page_size"], 50)
        self.assertEqual(page["total"], 7)
        self.assertIsNone(page["next_cursor"])

    def test_full_scan_matches_search_exactly(self):
        expected = self.c.search()
        seen = []
        cursor = None
        pages = 0
        while True:
            page = self.c.search_page(page_size=2, cursor=cursor)
            seen.extend(page["items"])
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 4)  # 2+2+2+1
        self.assertEqual(seen, expected)  # 逐项同构且顺序一致，不重复不遗漏

    def test_last_page_next_cursor_is_none(self):
        page = self.c.search_page(page_size=200)
        self.assertEqual(len(page["items"]), 7)
        self.assertIsNone(page["next_cursor"])

    def test_empty_result_first_page(self):
        page = self.c.search_page("不存在的关键词xyz")
        self.assertEqual(page["items"], [])
        self.assertEqual(page["total"], 0)
        self.assertIsNone(page["next_cursor"])

    def test_filters_and_keyword_paginate(self):
        hits = self.c.search("schema", doc_type="schema")
        page = self.c.search_page("schema", doc_type="schema", page_size=200)
        self.assertEqual(page["items"], hits)
        self.assertEqual(page["total"], len(hits))

    def test_error_type_and_code(self):
        self.assertTrue(issubclass(SearchQueryInvalid, CatalogError))
        self.assertIs(SearchQueryInvalid, ErrorsSearchQueryInvalid)
        self.assertEqual(SearchQueryInvalid.code, "SearchQueryInvalid")
        with self.assertRaises(SearchQueryInvalid) as ctx:
            self.c.search_page(page_size=0)
        self.assertEqual(ctx.exception.code, "SearchQueryInvalid")


class SearchPageValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_page_size_rejects_non_int(self):
        for bad in (True, False, 1.5, "3", None, [3]):
            with self.assertRaises(SearchQueryInvalid, msg=repr(bad)):
                self.c.search_page(page_size=bad)

    def test_page_size_rejects_out_of_range(self):
        for bad in (0, -1, 201, 10**6):
            with self.assertRaises(SearchQueryInvalid, msg=repr(bad)):
                self.c.search_page(page_size=bad)

    def test_page_size_accepts_bounds(self):
        self.assertEqual(self.c.search_page(page_size=1)["page_size"], 1)
        self.assertEqual(self.c.search_page(page_size=200)["page_size"], 200)

    def test_non_string_keyword_rejected(self):
        for bad in (1, 1.5, ["x"], {"x": 1}, True):
            with self.assertRaises(SearchQueryInvalid, msg=repr(bad)):
                self.c.search_page(bad)

    def test_non_string_filter_rejected(self):
        for name in ("doc_type", "schema", "version", "field_path",
                     "change_kind", "compatibility", "asset_name"):
            with self.assertRaises(SearchQueryInvalid, msg=name):
                self.c.search_page(**{name: 123})

    def test_limit_and_unknown_kwargs_rejected(self):
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page(limit=3)
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page(offset=0)

    def test_cursor_rejects_missing_or_malformed(self):
        for bad in ("", "not-a-cursor", "mcsp1", "mcsp1.x", "mcsp1.x.y.z",
                    "xxxxx.aaaabbbb.cccc", 123, b"x", True):
            with self.assertRaises(SearchQueryInvalid, msg=repr(bad)):
                self.c.search_page(cursor=bad)

    def test_cursor_tampering_rejected(self):
        cursor = self.c.search_page(page_size=2)["next_cursor"]
        magic, payload, digest = cursor.split(".")
        # 篡改内容或校验值都不得通过
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page(page_size=2, cursor=f"{magic}.{payload[:-1]}a.{digest}")
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page(page_size=2, cursor=f"{magic}.{payload}.{'0' * len(digest)}")

    def test_cursor_bound_to_query_and_page_size(self):
        cursor = self.c.search_page(page_size=2)["next_cursor"]
        # 原查询可续页
        self.c.search_page(page_size=2, cursor=cursor)
        # 任一条件或 page_size 变化都不得复用
        for kwargs in (
            {"page_size": 3},
            {"page_size": 2, "doc_type": "schema"},
            {"page_size": 2, "schema": "S01"},
            {"page_size": 2, "version": "1.0"},
            {"page_size": 2, "field_path": "/a"},
            {"page_size": 2, "change_kind": "added"},
            {"page_size": 2, "compatibility": "breaking"},
            {"page_size": 2, "asset_name": "svc"},
        ):
            with self.assertRaises(SearchQueryInvalid, msg=str(kwargs)):
                self.c.search_page(cursor=cursor, **kwargs)
        with self.assertRaises(SearchQueryInvalid):
            self.c.search_page("schema", page_size=2, cursor=cursor)


class SearchPageConcurrencyTests(unittest.TestCase):
    def test_new_docs_during_pagination(self):
        c = build_catalog(5)
        first = c.search_page(page_size=2)
        self.assertEqual([h["name"] for h in first["items"]], ["S00", "S01"])
        cursor = first["next_cursor"]
        # 排在续页位置之前的新文档不进入后续页
        c.register_schema("A00", "1.0", {"type": "object"})
        # 排在续页位置之后的新文档进入后续页
        c.register_schema("S99", "1.0", {"type": "object"})
        rest = []
        while cursor is not None:
            page = c.search_page(page_size=2, cursor=cursor)
            rest.extend(h["name"] for h in page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(rest, ["S02", "S03", "S04", "S99"])

    def test_total_reflects_call_time_index(self):
        c = build_catalog(5)
        first = c.search_page(page_size=2)
        self.assertEqual(first["total"], 5)
        c.register_schema("S99", "1.0", {"type": "object"})
        second = c.search_page(page_size=2, cursor=first["next_cursor"])
        self.assertEqual(second["total"], 6)

    def test_completed_pages_unchanged_and_no_duplicates(self):
        c = build_catalog(4)
        p1 = c.search_page(page_size=2)
        c.register_schema("S05", "1.0", {"type": "object"})
        p2 = c.search_page(page_size=2, cursor=p1["next_cursor"])
        p3 = c.search_page(page_size=2, cursor=p2["next_cursor"])
        names = [h["name"] for h in p1["items"] + p2["items"] + p3["items"]]
        self.assertEqual(names, ["S00", "S01", "S02", "S03", "S05"])
        self.assertIsNone(p3["next_cursor"])


class SearchPageReadOnlyTests(unittest.TestCase):
    def test_search_page_does_not_change_existing_behavior(self):
        c = build_catalog(3)
        before_search = c.search()
        before_versions = c.list_versions("S00")
        c.search_page(page_size=1)
        c.search_page(page_size=1, cursor=c.search_page(page_size=1)["next_cursor"])
        self.assertEqual(c.search(), before_search)
        self.assertEqual(c.list_versions("S00"), before_versions)
        self.assertEqual(c.list_reports(), [])

    def test_normal_queries_never_raise(self):
        c = MetaCatalog()
        # 空目录、无命中、末页、跨页读取均不报错
        page = c.search_page()
        self.assertEqual(page, {"items": [], "total": 0, "page_size": 50,
                                "next_cursor": None})
        self.assertIsNone(c.search_page("nothing")["next_cursor"])


if __name__ == "__main__":
    unittest.main()
