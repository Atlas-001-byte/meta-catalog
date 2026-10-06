import json
import unittest

from meta_catalog import MetaCatalog
from meta_catalog.errors import (
    NotFoundError,
    SchemaComparisonInvalid,
)


def obj(**props_and_req):
    req = props_and_req.pop("__required__", [])
    doc = {"type": "object", "properties": props_and_req}
    if req:
        doc["required"] = req
    return doc


class PreviewTestBase(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema(
            "Address",
            "1.0",
            obj(
                street={"type": "string"},
                zip={"type": "string"},
                code={"type": "string", "enum": ["a", "b"]},
            ),
        )
        self.c.register_schema(
            "Person",
            "1.0",
            obj(
                name={"type": "string"},
                address={"$ref": "Address@1.0#"},
            ),
        )
        self.c.register_asset(
            "svc-direct", "直接服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/street"}],
        )
        self.c.register_asset(
            "svc-trans", "传递服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/address/street"}],
        )
        self.c.register_asset(
            "svc-ok", "无关服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/name"}],
        )


class PreviewShapeTests(PreviewTestBase):
    def test_empty_changes(self):
        out = self.c.preview_changes([])
        self.assertEqual(
            list(out.keys()),
            ["accepted", "conflicts", "impactedItems", "searchPreview"],
        )
        self.assertIs(out["accepted"], True)
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(out["impactedItems"], {"direct": [], "transitive": []})
        self.assertEqual(
            out["searchPreview"], {"added": [], "removed": [], "replaced": []}
        )

    def test_result_is_json_serializable_and_deterministic(self):
        changes = [
            {"schema": "Address", "version": "1.0", "changeType": "remove_field",
             "path": "/street"},
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/country", "definition": {"type": "string"}},
        ]
        first = self.c.preview_changes(changes)
        second = self.c.preview_changes(list(reversed(changes)))
        json.dumps(first, ensure_ascii=False)
        # 集合排序与输入顺序无关（source.index 除外时内容一致）。
        self.assertEqual(first, self.c.preview_changes(changes))
        self.assertEqual(
            [i["asset_id"] for i in first["impactedItems"]["direct"]],
            [i["asset_id"] for i in second["impactedItems"]["direct"]],
        )


class PreviewValidationTests(PreviewTestBase):
    def test_changes_must_be_list(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.preview_changes({"changeType": "add_field"})

    def test_missing_fields(self):
        base = {"schema": "Address", "version": "1.0", "changeType": "remove_field",
                "path": "/street"}
        for key in ("schema", "version", "changeType", "path"):
            bad = {k: v for k, v in base.items() if k != key}
            with self.assertRaises(SchemaComparisonInvalid, msg=key):
                self.c.preview_changes([bad])
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.preview_changes(
                [{"schema": "Address", "version": "1.0", "changeType": "add_field",
                  "path": "/x"}]
            )
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.preview_changes(
                [{"schema": "Address", "version": "1.0",
                  "changeType": "compatibility_check"}]
            )

    def test_unknown_change_type(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.preview_changes(
                [{"schema": "Address", "version": "1.0",
                  "changeType": "drop_everything"}]
            )

    def test_invalid_pointer(self):
        for bad in ("street", 42, None):
            with self.assertRaises(SchemaComparisonInvalid, msg=repr(bad)):
                self.c.preview_changes(
                    [{"schema": "Address", "version": "1.0",
                      "changeType": "remove_field", "path": bad}]
                )

    def test_invalid_definition_and_candidate(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.preview_changes(
                [{"schema": "Address", "version": "1.0", "changeType": "add_field",
                  "path": "/x", "definition": {"type": "nope"}}]
            )
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.preview_changes(
                [{"schema": "Address", "version": "1.0",
                  "changeType": "compatibility_check",
                  "candidate": {"type": "nope"}}]
            )

    def test_duplicate_path_content_mismatch(self):
        dup = [
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/x", "definition": {"type": "string"}},
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/x", "definition": {"type": "integer"}},
        ]
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.preview_changes(dup)

    def test_identical_duplicates_are_deduped(self):
        one = {"schema": "Address", "version": "1.0", "changeType": "add_field",
               "path": "/x", "definition": {"type": "string"}}
        out = self.c.preview_changes([one, dict(one)])
        self.assertIs(out["accepted"], True)
        self.assertEqual(len(out["searchPreview"]["replaced"]), 1)

    def test_unknown_schema_or_version(self):
        with self.assertRaises(NotFoundError):
            self.c.preview_changes(
                [{"schema": "Nope", "version": "1.0",
                  "changeType": "unregister_schema"}]
            )
        with self.assertRaises(NotFoundError):
            self.c.preview_changes(
                [{"schema": "Address", "version": "9.9",
                  "changeType": "remove_field", "path": "/street"}]
            )

    def test_invalid_search_argument(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.preview_changes([], search="keyword")
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.preview_changes([], search={"bogus_filter": 1})


class PreviewConflictTests(PreviewTestBase):
    def test_remove_and_change_type_on_unregistered_field(self):
        out = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "remove_field",
             "path": "/nope"},
            {"schema": "Address", "version": "1.0", "changeType": "change_type",
             "path": "/also-nope", "definition": {"type": "string"}},
        ])
        self.assertIs(out["accepted"], False)
        self.assertEqual(
            [c["code"] for c in out["conflicts"]],
            ["FIELD_NOT_FOUND", "FIELD_NOT_FOUND"],
        )
        self.assertEqual([c["index"] for c in out["conflicts"]], [0, 1])
        resources = out["conflicts"][0]["resources"]
        self.assertEqual(resources[0]["path"], "/nope")
        self.assertEqual(resources[0]["schema"], "Address")
        self.assertEqual(out["impactedItems"], {"direct": [], "transitive": []})
        self.assertEqual(
            out["searchPreview"], {"added": [], "removed": [], "replaced": []}
        )

    def test_same_path_different_change_types(self):
        out = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "remove_field",
             "path": "/street"},
            {"schema": "Address", "version": "1.0", "changeType": "change_type",
             "path": "/street", "definition": {"type": "integer"}},
        ])
        self.assertIs(out["accepted"], False)
        self.assertEqual([c["code"] for c in out["conflicts"]], ["CHANGE_CONFLICT"])
        self.assertEqual(out["conflicts"][0]["index"], 1)
        self.assertEqual(len(out["conflicts"][0]["resources"]), 2)

    def test_add_field_on_registered_field(self):
        out = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/street", "definition": {"type": "string"}},
        ])
        self.assertIs(out["accepted"], False)
        self.assertEqual([c["code"] for c in out["conflicts"]], ["CHANGE_CONFLICT"])

    def test_unregister_with_other_changes_on_same_version(self):
        out = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "unregister_schema"},
            {"schema": "Address", "version": "1.0", "changeType": "remove_field",
             "path": "/zip"},
        ])
        self.assertIs(out["accepted"], False)
        self.assertEqual([c["code"] for c in out["conflicts"]], ["CHANGE_CONFLICT"])
        # 其他版本的变更不受牵连。
        out2 = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "unregister_schema"},
            {"schema": "Person", "version": "1.0", "changeType": "remove_field",
             "path": "/name"},
        ])
        self.assertIs(out2["accepted"], True)


class PreviewImpactTests(PreviewTestBase):
    def test_remove_field_direct_and_transitive(self):
        out = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "remove_field",
             "path": "/street"},
        ])
        self.assertIs(out["accepted"], True)
        direct = out["impactedItems"]["direct"]
        self.assertEqual([d["asset_id"] for d in direct], ["svc-direct"])
        self.assertEqual(direct[0]["path"], "/street")
        self.assertEqual(direct[0]["compatibility"], "breaking")
        self.assertEqual(
            direct[0]["source"], {"index": 0, "changeType": "remove_field"}
        )
        transitive = out["impactedItems"]["transitive"]
        self.assertEqual([t["asset_id"] for t in transitive], ["svc-trans"])
        self.assertEqual(transitive[0]["schema"], "Person")
        self.assertEqual(transitive[0]["matched_paths"], ["/street"])

    def test_change_type_compatibility_classification(self):
        breaking = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "change_type",
             "path": "/street", "definition": {"type": "integer"}},
        ])
        self.assertEqual(
            breaking["impactedItems"]["direct"][0]["compatibility"], "breaking"
        )
        metadata = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "change_type",
             "path": "/street",
             "definition": {"type": "string", "description": "街道"}},
        ])
        self.assertEqual(
            metadata["impactedItems"]["direct"][0]["compatibility"], "metadata"
        )

    def test_add_field_has_no_impact(self):
        out = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/country", "definition": {"type": "string"}},
        ])
        self.assertIs(out["accepted"], True)
        self.assertEqual(out["impactedItems"], {"direct": [], "transitive": []})

    def test_unregister_schema_impacts(self):
        out = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "unregister_schema"},
        ])
        self.assertIs(out["accepted"], True)
        self.assertEqual(
            [d["asset_id"] for d in out["impactedItems"]["direct"]], ["svc-direct"]
        )
        self.assertEqual(
            [t["asset_id"] for t in out["impactedItems"]["transitive"]],
            ["svc-trans"],
        )

    def test_compatibility_check_impacts_and_extension_fields(self):
        candidate = obj(
            street={"type": "integer"},                # 收窄：breaking
            zip={"type": "string", "description": "邮编"},  # metadata
            code={"type": "string", "enum": ["a", "b", "c"]},  # 放宽：compatible
            country={"type": "string"},
            __required__=["country"],  # 旧版本未定义的扩展字段：不新增要求
        )
        out = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "compatibility_check", "candidate": candidate},
        ])
        self.assertIs(out["accepted"], True)
        direct = out["impactedItems"]["direct"]
        by_asset = {d["asset_id"]: d for d in direct}
        self.assertEqual(by_asset["svc-direct"]["compatibility"], "breaking")
        # 扩展字段（含必填）按 compatible 计入，不产生资产影响。
        added = [
            e for e in out["searchPreview"]["added"] if e["path"] == "/country"
        ]
        self.assertEqual(len(added), 1)
        # 影响条目按 asset_id 稳定排序。
        self.assertEqual(
            [d["asset_id"] for d in direct], sorted(d["asset_id"] for d in direct)
        )


class PreviewSearchPreviewTests(PreviewTestBase):
    def test_add_remove_change_type_replace_schema_doc(self):
        out = self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/country", "definition": {"type": "string"}},
            {"schema": "Person", "version": "1.0", "changeType": "remove_field",
             "path": "/name"},
        ])
        replaced = out["searchPreview"]["replaced"]
        self.assertEqual(
            [r["id"] for r in replaced],
            ["schema:Address@1.0", "schema:Person@1.0"],
        )
        self.assertEqual([r["type"] for r in replaced], ["schema", "schema"])
        self.assertEqual(
            [r["reason"] for r in replaced], ["add_field", "remove_field"]
        )
        self.assertEqual(out["searchPreview"]["added"], [])
        self.assertEqual(out["searchPreview"]["removed"], [])

    def test_unregister_removes_schema_doc(self):
        out = self.c.preview_changes([
            {"schema": "Address", "version": "1.0",
             "changeType": "unregister_schema"},
        ])
        self.assertEqual(
            [r["id"] for r in out["searchPreview"]["removed"]],
            ["schema:Address@1.0"],
        )
        self.assertEqual(out["searchPreview"]["replaced"], [])

    def test_compatibility_check_adds_change_docs(self):
        candidate = obj(
            street={"type": "integer"},
            zip={"type": "string"},
            code={"type": "string", "enum": ["a", "b"]},
        )
        changes = [
            {"schema": "Address", "version": "1.0",
             "changeType": "compatibility_check", "candidate": candidate},
        ]
        out = self.c.preview_changes(changes)
        added = out["searchPreview"]["added"]
        self.assertEqual(len(added), 1)  # 仅 /street 一条变更
        self.assertEqual(added[0]["type"], "change")
        self.assertEqual(added[0]["path"], "/street")
        self.assertEqual(added[0]["reason"], "compatibility_check")
        # 预测的 report_id 与相同输入的 compare_schemas 一致。
        report = self.c.compare_schemas("Address", "1.0", candidate)
        self.assertEqual(added[0]["report_id"], report["report_id"])

    def test_search_filter_limits_preview(self):
        changes = [
            {"schema": "Address", "version": "1.0", "changeType": "add_field",
             "path": "/country", "definition": {"type": "string"}},
            {"schema": "Person", "version": "1.0", "changeType": "remove_field",
             "path": "/name"},
        ]
        out = self.c.preview_changes(changes, search={"schema": "Person"})
        self.assertEqual(
            [r["id"] for r in out["searchPreview"]["replaced"]],
            ["schema:Person@1.0"],
        )
        none = self.c.preview_changes(changes, search={"schema": "Nope"})
        self.assertEqual(none["searchPreview"]["replaced"], [])

    def test_search_filter_on_added_change_docs(self):
        candidate = obj(
            street={"type": "integer"},                       # breaking
            zip={"type": "string", "description": "邮编"},      # metadata
            code={"type": "string", "enum": ["a", "b", "c"]},  # compatible
        )
        changes = [
            {"schema": "Address", "version": "1.0",
             "changeType": "compatibility_check", "candidate": candidate},
        ]
        out = self.c.preview_changes(
            changes, search={"doc_type": "change", "compatibility": "breaking"}
        )
        self.assertEqual(
            [a["path"] for a in out["searchPreview"]["added"]], ["/street"]
        )


class PreviewReadOnlyTests(PreviewTestBase):
    def test_preview_does_not_mutate_catalog(self):
        before_search = self.c.search()
        before_versions = self.c.list_versions("Address")
        before_schema = self.c.get_schema("Address", "1.0")

        self.c.preview_changes([
            {"schema": "Address", "version": "1.0", "changeType": "remove_field",
             "path": "/street"},
            {"schema": "Address", "version": "1.0",
             "changeType": "compatibility_check",
             "candidate": obj(street={"type": "integer"}, zip={"type": "string"},
                              code={"type": "string"})},
            {"schema": "Person", "version": "1.0", "changeType": "add_field",
             "path": "/email", "definition": {"type": "string"}},
        ])

        self.assertEqual(self.c.search(), before_search)
        self.assertEqual(self.c.list_versions("Address"), before_versions)
        self.assertEqual(self.c.get_schema("Address", "1.0"), before_schema)
        self.assertEqual(self.c.list_reports(), [])
        # 预检的候选文档未被注册。
        with self.assertRaises(NotFoundError):
            self.c.get_schema("Address", "2.0")


if __name__ == "__main__":
    unittest.main()
