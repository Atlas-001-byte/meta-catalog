import unittest

from meta_catalog import schema_fields as sf


def classify(old, new):
    return sf.classify_pair({"type": "object", "properties": {"x": old}},
                            {"type": "object", "properties": {"x": new}}) if False else \
           sf.classify_pair(old, new)


class ClassificationTests(unittest.TestCase):
    def test_integer_to_number_is_compatible(self):
        self.assertEqual(
            sf.classify_pair({"type": "integer"}, {"type": "number"}), "compatible"
        )

    def test_number_to_integer_is_breaking(self):
        self.assertEqual(
            sf.classify_pair({"type": "number"}, {"type": "integer"}), "breaking"
        )

    def test_adding_null_is_breaking(self):
        self.assertEqual(
            sf.classify_pair(
                {"type": "string"}, {"type": ["string", "null"]}
            ),
            "breaking",
        )

    def test_enum_widening_compatible_narrowing_breaking(self):
        old = {"type": "string", "enum": ["a", "b"]}
        self.assertEqual(
            sf.classify_pair(old, {"type": "string", "enum": ["a", "b", "c"]}),
            "compatible",
        )
        self.assertEqual(
            sf.classify_pair(old, {"type": "string", "enum": ["a"]}), "breaking"
        )

    def test_removing_enum_is_compatible(self):
        self.assertEqual(
            sf.classify_pair(
                {"type": "string", "enum": ["a"]}, {"type": "string"}
            ),
            "compatible",
        )

    def test_adding_enum_is_breaking(self):
        self.assertEqual(
            sf.classify_pair(
                {"type": "string"}, {"type": "string", "enum": ["a"]}
            ),
            "breaking",
        )

    def test_adding_default_is_compatible(self):
        self.assertEqual(
            sf.classify_pair({"type": "string"}, {"type": "string", "default": "x"}),
            "compatible",
        )

    def test_changing_default_is_breaking(self):
        self.assertEqual(
            sf.classify_pair(
                {"type": "string", "default": "a"},
                {"type": "string", "default": "b"},
            ),
            "breaking",
        )

    def test_annotation_only_is_metadata(self):
        self.assertEqual(
            sf.classify_pair(
                {"type": "string", "title": "A"},
                {"type": "string", "title": "B", "description": "d"},
            ),
            "metadata",
        )

    def test_bound_loosen_and_tighten(self):
        self.assertEqual(
            sf.classify_pair(
                {"type": "integer", "minimum": 10},
                {"type": "integer", "minimum": 5},
            ),
            "compatible",
        )
        self.assertEqual(
            sf.classify_pair(
                {"type": "integer", "maximum": 10},
                {"type": "integer", "maximum": 5},
            ),
            "breaking",
        )
        self.assertEqual(
            sf.classify_pair(
                {"type": "string", "maxLength": 10},
                {"type": "string"},
            ),
            "compatible",
        )
        self.assertEqual(
            sf.classify_pair(
                {"type": "string"},
                {"type": "string", "minLength": 3},
            ),
            "breaking",
        )

    def test_mixed_change_is_breaking(self):
        # 枚举放宽但类型收窄 -> breaking
        old = {"type": "number", "enum": [1, 2]}
        new = {"type": "integer", "enum": [1, 2, 3]}
        self.assertEqual(sf.classify_pair(old, new), "breaking")


class ExpandTests(unittest.TestCase):
    def test_required_paths_collected(self):
        doc = {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {
                    "type": "object",
                    "properties": {"c": {"type": "integer"}},
                    "required": ["c"],
                },
            },
            "required": ["a"],
        }
        fields = sf.expand(doc)
        self.assertIn("/a", fields)
        self.assertIn("/b", fields)
        self.assertIn("/b/c", fields)
        self.assertIn("/a", fields[""].required_paths)
        self.assertIn("/b/c", fields["/b"].required_paths)

    def test_allof_required_merged(self):
        doc = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "allOf": [{"required": ["a"]}],
        }
        fields = sf.expand(doc)
        self.assertIn("/a", fields[""].required_paths)

    def test_array_items_path(self):
        doc = {
            "type": "object",
            "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
        }
        fields = sf.expand(doc)
        self.assertIn("/tags/-", fields)


if __name__ == "__main__":
    unittest.main()
