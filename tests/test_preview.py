import copy
import json
import unittest

from meta_catalog import MetaCatalog, limits
from meta_catalog.errors import (
    ImpactAnalysisTooLarge,
    NotFoundError,
    SchemaComparisonInvalid,
)

EMPTY_IMPACT = {"direct": [], "transitive": []}
EMPTY_PREVIEW = {"added": [], "removed": [], "replaced": []}


def build_catalog():
    c = MetaCatalog()
    c.register_schema(
        "Address",
        "1.0",
        {
            "type": "object",
            "properties": {
                "street": {"type": "string"},
                "zip": {"type": "string", "enum": ["a", "b"]},
            },
            "required": ["street"],
        },
    )
    c.register_schema(
        "Person",
        "1.0",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "address": {"$ref": "Address@1.0#"},
            },
        },
    )
    c.register_asset(
        "svc-direct", "直接服务", "service",
        [{"schema": "Address", "version": "1.0", "path": "/street"}],
    )
    c.register_asset(
        "svc-trans", "传递服务", "service",
        [{"schema": "Person", "version": "1.0", "path": "/address/street"}],
    )
    c.register_asset(
        "svc-other", "无关服务", "service",
        [{"schema": "Person", "version": "1.0", "path": "/name"}],
    )
    return c


class PreviewShapeTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_empty_changes_accepted_with_empty_collections(self):
        rep = self.c.preview_changes([])
        self.assertEqual(
            rep,
            {
                "accepted": True,
                "conflicts": [],
                "impactedItems": EMPTY_IMPACT,
                "searchPreview": EMPTY_PREVIEW,
            },
        )
        self.assertTrue(json.dumps(rep, ensure_ascii=False))

    def test_top_level_key_order(self):
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/email", "definition": {"type": "string"}},
        ])
        self.assertEqual(
            list(rep.keys()),
            ["accepted", "conflicts", "impactedItems", "searchPreview"],
        )
        self.assertEqual(
            list(rep["impactedItems"].keys()), ["direct", "transitive"]
        )
        self.assertEqual(
            list(rep["searchPreview"].keys()), ["added", "removed", "replaced"]
        )


class PreviewAcceptedTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def test_add_field_has_no_impacts_and_replaces_schema_doc(self):
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/email", "definition": {"type": "string"}},
        ])
        self.assertTrue(rep["accepted"])
        self.assertEqual(rep["conflicts"], [])
        self.assertEqual(rep["impactedItems"], EMPTY_IMPACT)
        (entry,) = rep["searchPreview"]["replaced"]
        self.assertEqual(entry, {
            "id": "schema:Address@1.0",
            "type": "schema",
            "schema": "Address",
            "version": "1.0",
            "report_id": None,
            "path": None,
            "index": 0,
            "reason": "add_field",
        })
        self.assertEqual(rep["searchPreview"]["added"], [])
        self.assertEqual(rep["searchPreview"]["removed"], [])

    def test_remove_field_breaking_direct_and_transitive(self):
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "remove_field", "path": "/street"},
        ])
        self.assertTrue(rep["accepted"])
        direct, trans = rep["impactedItems"]["direct"], rep["impactedItems"]["transitive"]
        self.assertEqual([d["asset_id"] for d in direct], ["svc-direct"])
        self.assertEqual(direct[0]["compatibility"], "breaking")
        self.assertEqual(direct[0]["path"], "/street")
        self.assertEqual(direct[0]["source"],
                         {"index": 0, "changeType": "remove_field"})
        self.assertEqual([t["asset_id"] for t in trans], ["svc-trans"])
        self.assertEqual(trans[0]["schema"], "Person")
        self.assertEqual(trans[0]["path"], "/address/street")
        self.assertEqual(trans[0]["compatibility"], "breaking")
        # 无关资产不受影响
        hit_ids = {x["asset_id"] for x in direct + trans}
        self.assertNotIn("svc-other", hit_ids)

    def test_change_type_classifies_compatibility(self):
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "change_type",
             "path": "/street", "definition": {"type": "integer"}},
        ])
        self.assertTrue(rep["accepted"])
        self.assertEqual(rep["impactedItems"]["direct"][0]["compatibility"],
                         "breaking")

        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "change_type",
             "path": "/zip", "definition": {"type": "string",
                                            "enum": ["a", "b", "c"]}},
        ])
        self.assertEqual(rep["impactedItems"]["direct"], [])
        self.assertEqual(rep["impactedItems"]["transitive"], [])

    def test_unregister_schema_removes_doc_and_lists_impacts(self):
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "unregister_schema"},
        ])
        self.assertTrue(rep["accepted"])
        (entry,) = rep["searchPreview"]["removed"]
        self.assertEqual(entry["id"], "schema:Address@1.0")
        self.assertEqual(entry["reason"], "unregister_schema")
        self.assertEqual(rep["searchPreview"]["replaced"], [])
        self.assertEqual(
            {d["asset_id"] for d in rep["impactedItems"]["direct"]},
            {"svc-direct"},
        )
        self.assertEqual(
            {t["asset_id"] for t in rep["impactedItems"]["transitive"]},
            {"svc-trans"},
        )

    def test_compatibility_check_preview_and_impacts(self):
        candidate = {
            "type": "object",
            "properties": {
                "street": {"type": "integer"},
                "zip": {"type": "string", "enum": ["a", "b"]},
                "email": {"type": "string"},
            },
            "required": ["street", "email"],
        }
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "compatibility_check", "candidate": candidate},
        ])
        self.assertTrue(rep["accepted"])
        added = rep["searchPreview"]["added"]
        self.assertEqual({a["path"] for a in added}, {"/street", "/email"})
        for a in added:
            self.assertEqual(a["type"], "change")
            self.assertEqual(a["reason"], "compatibility_check")
            self.assertTrue(a["id"].startswith("change:"))
        self.assertEqual(rep["searchPreview"]["replaced"], [])
        self.assertEqual(rep["searchPreview"]["removed"], [])
        # /street 收窄为 breaking，直接与传递资产各命中
        direct = rep["impactedItems"]["direct"]
        self.assertEqual({(d["path"], d["compatibility"]) for d in direct},
                         {("/street", "breaking")})
        self.assertEqual(
            {t["asset_id"] for t in rep["impactedItems"]["transitive"]},
            {"svc-trans"},
        )

    def test_identical_duplicate_entries_are_deduped(self):
        entry = {"schema": "Address", "version": "1.0",
                 "changeType": "add_field", "path": "/email",
                 "definition": {"type": "string"}}
        rep = self.c.preview_changes([entry, copy.deepcopy(entry)])
        self.assertTrue(rep["accepted"])
        self.assertEqual(len(rep["searchPreview"]["replaced"]), 1)


class PreviewConflictTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def _assert_empty_collections(self, rep):
        self.assertEqual(rep["impactedItems"], EMPTY_IMPACT)
        self.assertEqual(rep["searchPreview"], EMPTY_PREVIEW)

    def test_add_field_on_existing_field_is_conflict(self):
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/street", "definition": {"type": "string"}},
        ])
        self.assertFalse(rep["accepted"])
        self.assertEqual(rep["conflicts"][0]["code"], "CHANGE_CONFLICT")
        self._assert_empty_collections(rep)

    def test_remove_unknown_field_is_field_not_found(self):
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "remove_field", "path": "/ghost"},
        ])
        self.assertFalse(rep["accepted"])
        self.assertEqual(rep["conflicts"], [{
            "code": "FIELD_NOT_FOUND",
            "index": 0,
            "resources": [{
                "index": 0, "schema": "Address",
                "version": "1.0", "path": "/ghost",
            }],
        }])
        self._assert_empty_collections(rep)

    def test_change_type_unknown_field_is_field_not_found(self):
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "change_type",
             "path": "/ghost", "definition": {"type": "string"}},
        ])
        self.assertFalse(rep["accepted"])
        self.assertEqual(rep["conflicts"][0]["code"], "FIELD_NOT_FOUND")

    def test_same_path_different_change_types_is_batch_conflict(self):
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "remove_field", "path": "/street"},
            {"schema": "Address", "version": "1.0", "changeType": "change_type",
             "path": "/street", "definition": {"type": "integer"}},
        ])
        self.assertFalse(rep["accepted"])
        self.assertTrue(rep["conflicts"])
        self.assertTrue(all(c["code"] == "CHANGE_CONFLICT"
                            for c in rep["conflicts"]))
        self._assert_empty_collections(rep)

    def test_unregister_with_other_version_changes_is_conflict(self):
        rep = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "unregister_schema"},
            {"schema": "Address", "version": "1.0",
             "changeType": "remove_field", "path": "/street"},
        ])
        self.assertFalse(rep["accepted"])
        self.assertTrue(rep["conflicts"])
        self.assertTrue(all(c["code"] == "CHANGE_CONFLICT"
                            for c in rep["conflicts"]))


class PreviewSearchFilterTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()
        self.candidate = {
            "type": "object",
            "properties": {
                "street": {"type": "integer"},
                "email": {"type": "string"},
            },
            "required": ["street"],
        }

    def test_structured_filter_keeps_matching_schema_entry(self):
        changes = [
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/email", "definition": {"type": "string"}},
        ]
        rep = self.c.preview_changes(changes, search={"schema": "Address"})
        self.assertEqual(len(rep["searchPreview"]["replaced"]), 1)

        rep = self.c.preview_changes(changes, search={"schema": "Person"})
        self.assertEqual(rep["searchPreview"], EMPTY_PREVIEW)

    def test_keyword_simulated_on_temporary_index(self):
        changes = [
            {"schema": "Address", "version": "1.0",
             "changeType": "compatibility_check", "candidate": self.candidate},
        ]
        rep = self.c.preview_changes(changes, search={"keyword": "street"})
        self.assertEqual({a["path"] for a in rep["searchPreview"]["added"]},
                         {"/street"})
        rep = self.c.preview_changes(changes,
                                     search={"keyword": "zzznomatch"})
        self.assertEqual(rep["searchPreview"]["added"], [])


class PreviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()

    def assertInvalid(self, changes, **kw):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.preview_changes(changes, **kw)

    def test_changes_must_be_list(self):
        self.assertInvalid({"not": "a list"})

    def test_change_must_be_object(self):
        self.assertInvalid(["nope"])

    def test_missing_required_fields(self):
        self.assertInvalid([{"version": "1.0", "changeType": "add_field"}])
        self.assertInvalid([{"schema": "Address", "changeType": "add_field"}])
        self.assertInvalid([{"schema": "Address", "version": "1.0"}])
        self.assertInvalid([
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/x"},
        ])
        self.assertInvalid([
            {"schema": "Address", "version": "1.0",
             "changeType": "compatibility_check"},
        ])

    def test_unknown_change_type(self):
        self.assertInvalid([
            {"schema": "Address", "version": "1.0", "changeType": "bogus"},
        ])

    def test_invalid_pointer(self):
        self.assertInvalid([
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "no-leading-slash", "definition": {"type": "string"}},
        ])

    def test_definition_must_be_schema(self):
        self.assertInvalid([
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/x", "definition": ["not", "schema"]},
        ])

    def test_candidate_must_be_schema(self):
        self.assertInvalid([
            {"schema": "Address", "version": "1.0",
             "changeType": "compatibility_check", "candidate": "nope"},
        ])

    def test_duplicate_path_content_mismatch(self):
        self.assertInvalid([
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/x", "definition": {"type": "string"}},
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/x", "definition": {"type": "integer"}},
        ])

    def test_unknown_search_filter(self):
        self.assertInvalid([], search={"unknown_filter": 1})

    def test_search_must_be_mapping(self):
        self.assertInvalid([], search=["nope"])

    def test_unknown_schema_or_version_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.c.preview_changes([
                {"schema": "Ghost", "version": "1.0",
                 "changeType": "unregister_schema"},
            ])
        with self.assertRaises(NotFoundError):
            self.c.preview_changes([
                {"schema": "Address", "version": "9.9",
                 "changeType": "unregister_schema"},
            ])


class PreviewLimitsTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema(
            "A", "1.0",
            {"type": "object",
             "properties": {"x": {"type": "string"}, "y": {"type": "string"}}},
        )
        self.c.register_asset(
            "a1", "资产", "service",
            [{"schema": "A", "version": "1.0", "path": "/x"}],
        )

    def test_impact_asset_limit(self):
        old = limits.MAX_IMPACT_ASSETS
        limits.MAX_IMPACT_ASSETS = 0
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                self.c.preview_changes([
                    {"schema": "A", "version": "1.0",
                     "changeType": "remove_field", "path": "/x"},
                ])
            self.assertEqual(cm.exception.details["reason"], "assets_exceeded")
        finally:
            limits.MAX_IMPACT_ASSETS = old

    def test_change_count_limit(self):
        candidate = {"type": "object", "properties": {
            "x": {"type": "string"},
            "z": {"type": "string"},
        }}
        old = limits.MAX_CHANGES
        limits.MAX_CHANGES = 1
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                self.c.preview_changes([
                    {"schema": "A", "version": "1.0",
                     "changeType": "compatibility_check", "candidate": candidate},
                ])
            self.assertEqual(cm.exception.details["reason"], "changes_exceeded")
        finally:
            limits.MAX_CHANGES = old


class PreviewReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.c = build_catalog()
        self.candidate = {
            "type": "object",
            "properties": {
                "street": {"type": "integer"},
                "email": {"type": "string"},
            },
            "required": ["street"],
        }

    def test_no_registration_no_report_no_index_mutation(self):
        versions_before = self.c.list_versions("Address")
        search_before = self.c.search()
        n_reports = len(self.c.list_reports())
        self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "add_field", "path": "/email",
             "definition": {"type": "string"}},
            {"schema": "Address", "version": "1.0",
             "changeType": "remove_field", "path": "/street"},
            {"schema": "Address", "version": "1.0",
             "changeType": "compatibility_check", "candidate": self.candidate},
            {"schema": "Address", "version": "1.0",
             "changeType": "unregister_schema"},
        ])
        self.assertEqual(self.c.list_versions("Address"), versions_before)
        self.assertEqual(len(self.c.list_reports()), n_reports)
        self.assertEqual(self.c.search(), search_before)

    def test_result_is_independent_copy(self):
        changes = [
            {"schema": "Address", "version": "1.0",
             "changeType": "remove_field", "path": "/street"},
        ]
        r1 = self.c.preview_changes(changes)
        r1["impactedItems"]["direct"].clear()
        r2 = self.c.preview_changes(changes)
        self.assertEqual(len(r2["impactedItems"]["direct"]), 1)

    def test_deterministic(self):
        changes = [
            {"schema": "Address", "version": "1.0",
             "changeType": "remove_field", "path": "/street"},
        ]
        self.assertEqual(
            self.c.preview_changes(copy.deepcopy(changes)),
            self.c.preview_changes(copy.deepcopy(changes)),
        )


if __name__ == "__main__":
    unittest.main()
