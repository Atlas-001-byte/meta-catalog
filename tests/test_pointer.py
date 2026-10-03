import unittest

from meta_catalog import pointer as ptr


class PointerTests(unittest.TestCase):
    def test_parse_format_roundtrip(self):
        for p in ("", "/", "/a", "/a/b", "/a~1b", "/m~0n"):
            if p == "/":
                continue
            self.assertEqual(ptr.format(ptr.parse(p)), p)

    def test_parse_root(self):
        self.assertEqual(ptr.parse(""), ())

    def test_escape(self):
        self.assertEqual(ptr.escape("a/b"), "a~1b")
        self.assertEqual(ptr.escape("m~n"), "m~0n")
        self.assertEqual(ptr.unescape("a~1b"), "a/b")

    def test_child_and_parent(self):
        self.assertEqual(ptr.child("", "a"), "/a")
        self.assertEqual(ptr.child("/a", "b"), "/a/b")
        self.assertEqual(ptr.parent("/a/b"), "/a")
        self.assertIsNone(ptr.parent(""))

    def test_is_under(self):
        self.assertTrue(ptr.is_under("/a", "/a/b"))
        self.assertTrue(ptr.is_under("/a", "/a"))
        self.assertFalse(ptr.is_under("/a", "/ab"))
        self.assertTrue(ptr.is_under("", "/anything"))

    def test_resolve(self):
        doc = {"a": {"b": [10, 20]}}
        self.assertEqual(ptr.resolve(doc, "/a/b/1"), 20)
        with self.assertRaises(KeyError):
            ptr.resolve(doc, "/a/x")
        with self.assertRaises(KeyError):
            ptr.resolve(doc, "/a/b/9")

    def test_normalize_field_pointer(self):
        self.assertEqual(ptr.normalize_field_pointer("/properties/a"), "/a")
        self.assertEqual(
            ptr.normalize_field_pointer("/properties/a/properties/b"), "/a/b"
        )
        self.assertEqual(
            ptr.normalize_field_pointer("/properties/tags/items/properties/v"),
            "/tags/-/v",
        )
        self.assertEqual(
            ptr.normalize_field_pointer("/properties/additionalProperties"),
            "/*",
        )

    def test_resolve_logical(self):
        doc = {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}},
                "tags": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"v": {"type": "integer"}}},
                },
            },
        }
        # 名为 items 的属性与 items 关键字可被上下文正确区分。
        self.assertEqual(
            ptr.resolve_logical(doc, "/properties/items/items"), "/items/-"
        )
        self.assertEqual(
            ptr.resolve_logical(doc, "/properties/tags/items/properties/v"),
            "/tags/-/v",
        )
        self.assertIsNone(ptr.resolve_logical(doc, "/properties/missing"))
        # 根指针。
        self.assertEqual(ptr.resolve_logical(doc, ""), "")


if __name__ == "__main__":
    unittest.main()
