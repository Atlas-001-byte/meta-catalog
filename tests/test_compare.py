import unittest

from meta_catalog import MetaCatalog
from meta_catalog.errors import SchemaComparisonInvalid
from meta_catalog import compare as cmp_mod


def obj(**props_and_req):
    req = props_and_req.pop("__required__", [])
    doc = {"type": "object", "properties": props_and_req}
    if req:
        doc["required"] = req
    return doc


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.v1 = obj(
            name={"type": "string", "title": "名称"},
            age={"type": "integer"},
            level={"type": "string", "enum": ["a", "b"]},
            score={"type": "integer"},
            note={"type": "string", "description": "备注"},
            __required__=["name"],
        )
        self.c.register_schema("Person", "1.0", self.v1)

    def kinds(self, report):
        return {
            (ch["path"], ch["change_kind"]): ch["compatibility"]
            for ch in report["changes"]
        }

    def test_basic_change_kinds(self):
        v2 = obj(
            name={"type": "string", "title": "姓名"},        # metadata
            age={"type": "number"},                          # integer->number compatible
            level={"type": "string", "enum": ["a"]},         # enum narrowed breaking
            score={"type": "integer", "default": 0},         # default added compatible
            nick={"type": "string"},                         # optional added compatible
            __required__=["name", "age"],                    # age required added breaking
        )
        rep = self.c.compare_schemas("Person", "1.0", v2, candidate_version="2.0")
        k = self.kinds(rep)
        self.assertEqual(k[("/name", "metadata")], "metadata")
        # age 同时发生 integer→number 与新增必填：modified + breaking
        self.assertEqual(k[("/age", "modified")], "breaking")
        self.assertEqual(k[("/level", "modified")], "breaking")
        self.assertEqual(k[("/score", "modified")], "compatible")
        self.assertEqual(k[("/nick", "added")], "compatible")
        self.assertEqual(k[("/note", "deleted")], "breaking")
        self.assertEqual(
            rep["summary"],
            {"total": 6, "breaking": 3, "compatible": 2, "metadata": 1},
        )

    def test_existing_field_required_only_change(self):
        v2 = obj(
            name={"type": "string"},
            age={"type": "integer"},
            level={"type": "string", "enum": ["a", "b"]},
            score={"type": "integer"},
            note={"type": "string", "description": "备注"},
            __required__=["name", "age"],
        )
        rep = self.c.compare_schemas("Person", "1.0", v2)
        k = self.kinds(rep)
        self.assertEqual(k[("/age", "required_added")], "breaking")

    def test_existing_field_required_removed(self):
        v2 = obj(
            name={"type": "string"},
            age={"type": "integer"},
            level={"type": "string", "enum": ["a", "b"]},
            score={"type": "integer"},
            note={"type": "string", "description": "备注"},
        )
        rep = self.c.compare_schemas("Person", "1.0", v2)
        k = self.kinds(rep)
        self.assertEqual(k[("/name", "required_removed")], "compatible")

    def test_required_new_field_is_breaking(self):
        v2 = obj(
            name={"type": "string"},
            extra={"type": "string"},
            __required__=["name", "extra"],
        )
        rep = self.c.compare_schemas("Person", "1.0", v2)
        k = self.kinds(rep)
        self.assertEqual(k[("/extra", "added")], "breaking")

    def test_rename_single_entry_not_delete_plus_add(self):
        v2 = obj(
            name={"type": "string"},
            years={"type": "integer"},
            level={"type": "string", "enum": ["a", "b"]},
            score={"type": "integer"},
            note={"type": "string", "description": "备注"},
            __required__=["name"],
        )
        rep = self.c.compare_schemas(
            "Person", "1.0", v2, renames=[{"from": "/age", "to": "/years"}]
        )
        kinds = [(ch["path"], ch["change_kind"]) for ch in rep["changes"]]
        self.assertIn(("/years", "rename"), kinds)
        self.assertNotIn(("/age", "deleted"), kinds)
        self.assertNotIn(("/years", "added"), kinds)
        rename = next(ch for ch in rep["changes"] if ch["change_kind"] == "rename")
        self.assertEqual(rename["old_path"], "/age")
        self.assertEqual(rename["new_path"], "/years")
        self.assertEqual(rename["compatibility"], "breaking")
        self.assertIsNotNone(rename["old_summary"])
        self.assertIsNotNone(rename["new_summary"])

    def test_rename_uncovered_delete_and_add_still_present(self):
        v2 = obj(
            name={"type": "string"},
            level={"type": "string", "enum": ["a", "b"]},
            score={"type": "integer"},
            note={"type": "string", "description": "备注"},
            brandNew={"type": "string"},
            __required__=["name"],
        )
        rep = self.c.compare_schemas(
            "Person", "1.0", v2, renames=[{"from": "/age", "to": "/brandNew"}]
        )
        paths = {ch["path"] for ch in rep["changes"]}
        self.assertIn("/brandNew", paths)  # the rename root
        # age removed via rename, brandNew is the rename target
        rename_paths = {ch["new_path"] for ch in rep["changes"] if ch["change_kind"] == "rename"}
        self.assertEqual(rename_paths, {"/brandNew"})

    def test_rename_nested_subtree_aligned(self):
        v1 = obj(
            addr=obj(street={"type": "string"}, zip={"type": "string"}),
            name={"type": "string"},
        )
        self.c.register_schema("Order", "1.0", v1)
        v2 = obj(
            location=obj(street={"type": "string"}, zip={"type": "integer"}),
            name={"type": "string"},
        )
        rep = self.c.compare_schemas(
            "Order", "1.0", v2,
            renames=[{"from": "/addr", "to": "/location"}],
        )
        by_old = {ch["old_path"]: ch for ch in rep["changes"]}
        # exactly one rename at the root
        renames = [ch for ch in rep["changes"] if ch["change_kind"] == "rename"]
        self.assertEqual(len(renames), 1)
        self.assertEqual(renames[0]["old_path"], "/addr")
        # nested field aligned and compared
        self.assertEqual(by_old["/addr/zip"]["new_path"], "/location/zip")
        self.assertEqual(by_old["/addr/zip"]["change_kind"], "modified")
        self.assertEqual(by_old["/addr/zip"]["compatibility"], "breaking")
        # unchanged aligned child produces no entry
        self.assertNotIn("/addr/street", by_old)

    def test_invalid_rename_endpoints(self):
        v2 = obj(name={"type": "string"}, age={"type": "integer"},
                 level={"type": "string", "enum": ["a", "b"]},
                 score={"type": "integer"}, note={"type": "string"})
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.compare_schemas(
                "Person", "1.0", v2, renames=[{"from": "/missing", "to": "/age"}]
            )
        self.assertEqual(cm.exception.details["reason"], "rename_from_not_found")
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.compare_schemas(
                "Person", "1.0", v2, renames=[{"from": "/age", "to": "/missing"}]
            )
        self.assertEqual(cm.exception.details["reason"], "rename_to_not_found")

    def test_duplicate_mapping(self):
        v2 = obj(name={"type": "string"}, years={"type": "integer"},
                 era={"type": "integer"}, level={"type": "string", "enum": ["a", "b"]},
                 score={"type": "integer"}, note={"type": "string"})
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.compare_schemas(
                "Person", "1.0", v2,
                renames=[{"from": "/age", "to": "/years"},
                         {"from": "/score", "to": "/years"}],
            )
        self.assertEqual(cm.exception.details["reason"], "rename_duplicate")

    def test_cross_version_inconsistent(self):
        # rename source still present in candidate
        v2 = obj(name={"type": "string"}, age={"type": "integer"},
                 years={"type": "integer"}, level={"type": "string", "enum": ["a", "b"]},
                 score={"type": "integer"}, note={"type": "string"})
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.compare_schemas(
                "Person", "1.0", v2, renames=[{"from": "/age", "to": "/years"}]
            )
        self.assertEqual(cm.exception.details["reason"], "rename_cross_version_inconsistent")

    def test_baseline_version_not_found(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.compare_schemas("Person", "9.9", obj(a={}))
        self.assertEqual(cm.exception.details["reason"], "version_not_found")

    def test_candidate_version_not_found(self):
        with self.assertRaises(SchemaComparisonInvalid) as cm:
            self.c.compare_schemas("Person", "1.0", candidate_version="9.9")
        self.assertEqual(cm.exception.details["reason"], "version_not_found")

    def test_invalid_candidate_document(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.compare_schemas("Person", "1.0", ["not", "a", "schema"])

    def test_deterministic_field_order(self):
        v2 = obj(
            name={"type": "string"},
            age={"type": "number"},
            level={"type": "string", "enum": ["a", "b"]},
            score={"type": "integer"},
            note={"type": "string", "description": "x"},
            zeta={"type": "string"},
            __required__=["name"],
        )
        r1 = self.c.compare_schemas("Person", "1.0", v2, candidate_version="2.0")
        r2 = self.c.compare_schemas("Person", "1.0", v2, candidate_version="2.0")
        self.assertEqual(r1["report_id"], r2["report_id"])
        paths1 = [ch["path"] for ch in r1["changes"]]
        paths2 = [ch["path"] for ch in r2["changes"]]
        self.assertEqual(paths1, paths2)
        self.assertEqual(paths1, sorted(paths1, key=lambda p: p.split("/")))

    def test_report_id_idempotent_and_retrievable(self):
        v2 = obj(name={"type": "string"}, age={"type": "number"},
                 level={"type": "string", "enum": ["a", "b"]},
                 score={"type": "integer"}, note={"type": "string"})
        r1 = self.c.compare_schemas("Person", "1.0", v2)
        r2 = self.c.compare_schemas("Person", "1.0", v2)
        self.assertEqual(r1["report_id"], r2["report_id"])
        fetched = self.c.get_report(r1["report_id"])
        self.assertEqual(fetched["changes"], r1["changes"])


if __name__ == "__main__":
    unittest.main()
