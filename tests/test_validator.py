import unittest

from meta_catalog.errors import SchemaComparisonInvalid
from meta_catalog.validator import validate_schema


class ValidatorTests(unittest.TestCase):
    def test_accepts_minimal_schemas(self):
        validate_schema({})
        validate_schema(True)
        validate_schema(False)
        validate_schema({"type": "object"})

    def test_rejects_non_schema_root(self):
        for bad in ([], "string", 42, None):
            with self.assertRaises(SchemaComparisonInvalid) as cm:
                validate_schema(bad)
            self.assertEqual(cm.exception.details["reason"], "root_not_schema")

    def test_bad_type(self):
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"type": "widget"})
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"type": ["string", "string"]})

    def test_bad_required(self):
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"required": []})
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"required": ["a", "a"]})
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"required": [1]})

    def test_bad_enum(self):
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"enum": "x"})

    def test_bad_numeric_keywords(self):
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"minLength": -1})
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"minLength": True})
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"minimum": "1"})

    def test_bad_nested_composition(self):
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"allOf": []})
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"anyOf": [{"type": "x"}]})

    def test_internal_ref_resolvable(self):
        doc = {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/Name"}},
            "$defs": {"Name": {"type": "string"}},
        }
        validate_schema(doc)

    def test_internal_ref_unresolvable(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            validate_schema({"properties": {"a": {"$ref": "#/$defs/Missing"}}})
        self.assertEqual(cm.exception.details["reason"], "unresolvable_ref")

    def test_external_ref_allowed(self):
        validate_schema({"$ref": "Other@1.0#/properties/a"})

    def test_bad_pattern_properties(self):
        with self.assertRaises(SchemaComparisonInvalid):
            validate_schema({"patternProperties": {"[": {}}})


if __name__ == "__main__":
    unittest.main()
