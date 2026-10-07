"""``MetaCatalog.register_batch`` 原子批量登记测试。"""

import copy
import unittest

from meta_catalog import MetaCatalog, limits
from meta_catalog.errors import (
    AlreadyExistsError,
    BatchRegistrationInvalid,
    BatchRegistrationTooLarge,
    NotFoundError,
    SchemaComparisonInvalid,
)

ADDR_DOC = {"type": "object", "properties": {"street": {"type": "string"}}}


def person_doc():
    return {
        "type": "object",
        "properties": {
            "home": {"$ref": "Addr@1.0#/properties/street"},
            "work": {"$ref": "Company@1.0#"},
        },
    }


class RegisterBatchSuccessTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema("Addr", "1.0", ADDR_DOC)

    def test_empty_batch(self):
        self.assertEqual(self.c.register_batch([]), {"created": []})

    def test_mixed_batch_views_in_input_order(self):
        result = self.c.register_batch(
            [
                {"type": "schema", "name": "Person", "version": "1.0",
                 "document": person_doc()},
                {"type": "schema", "name": "Company", "version": "1.0",
                 "document": {
                     "type": "object",
                     "properties": {"ceo": {"$ref": "Person@1.0#"}}}},
                {"type": "asset", "asset_id": "svc-a", "name": "服务A",
                 "kind": "service",
                 "refs": [{"schema": "Person", "version": "1.0",
                           "path": "/properties/home"}]},
                {"type": "asset", "asset_id": "svc-b", "name": "服务B",
                 "kind": "job", "refs": []},
            ]
        )
        created = result["created"]
        self.assertEqual([x["type"] for x in created],
                         ["schema", "schema", "asset", "asset"])
        # 视图与 get_schema / get_asset 逐项一致。
        self.assertEqual(created[0],
                         {"type": "schema", **self.c.get_schema("Person", "1.0")})
        self.assertEqual(created[2],
                         {"type": "asset", **self.c.get_asset("svc-a")})
        self.assertEqual(created[3]["name"], "服务B")
        # 文档指针规整为逻辑路径。
        self.assertEqual(created[2]["refs"],
                         [{"schema": "Person", "version": "1.0", "path": "/home"}])
        # 立即可读。
        self.assertEqual(self.c.list_versions("Person"), ["1.0"])
        self.assertEqual(self.c.list_versions("Company"), ["1.0"])

    def test_asset_refs_resolve_against_batch_registry(self):
        # 资产引用本批 Schema 字段，并沿本批跨 Schema $ref 边传递可达。
        result = self.c.register_batch(
            [
                {"type": "schema", "name": "Person", "version": "1.0",
                 "document": person_doc()},
                {"type": "schema", "name": "Company", "version": "1.0",
                 "document": {"type": "object",
                              "properties": {"ceo": {"type": "string"}}}},
                {"type": "asset", "asset_id": "svc-x", "name": "X",
                 "kind": "service",
                 "refs": [{"schema": "Person", "version": "1.0",
                           "path": "/work/ceo"}]},
            ]
        )
        self.assertEqual(
            result["created"][2]["refs"],
            [{"schema": "Person", "version": "1.0", "path": "/work/ceo"}],
        )
        impact = self.c.analyze_impact("Company", "1.0", "/ceo")
        self.assertIn(
            "svc-x", {a["asset_id"] for a in impact["transitive_assets"]}
        )

    def test_forward_reference_allowed_in_batch(self):
        self.c.register_batch(
            [
                {"type": "schema", "name": "Fwd", "version": "1.0",
                 "document": {"type": "object", "properties": {
                     "q": {"$ref": "Later@2.0#/properties/q"}}}},
            ]
        )
        audit = self.c.check_schema_references("Fwd", "1.0")
        self.assertEqual(audit["issues"][0]["reason"], "missing_schema")
        # 目标随后单独注册，引用转为 resolved。
        self.c.register_schema(
            "Later", "2.0",
            {"type": "object", "properties": {"q": {"type": "string"}}},
        )
        audit = self.c.check_schema_references("Fwd", "1.0")
        self.assertEqual(audit["resolved"], 1)
        self.assertEqual(audit["issues"], [])

    def test_reference_cycle_in_batch(self):
        self.c.register_batch(
            [
                {"type": "schema", "name": "Ra", "version": "1",
                 "document": {"type": "object", "properties": {
                     "b": {"$ref": "Rb@1#"}, "x": {"type": "string"}}}},
                {"type": "schema", "name": "Rb", "version": "1",
                 "document": {"type": "object", "properties": {
                     "a": {"$ref": "Ra@1#/properties/x"}}}},
            ]
        )
        self.assertEqual(self.c.check_schema_references("Ra", "1")["resolved"], 1)
        self.assertEqual(self.c.check_schema_references("Rb", "1")["resolved"], 1)

    def test_versions_appended_in_input_order(self):
        self.c.register_schema("Seq", "1.0", {"type": "object"})
        self.c.register_batch(
            [
                {"type": "schema", "name": "Seq", "version": "3.0",
                 "document": {"type": "object"}},
                {"type": "schema", "name": "Seq", "version": "2.0",
                 "document": {"type": "object"}},
            ]
        )
        self.assertEqual(self.c.list_versions("Seq"), ["1.0", "3.0", "2.0"])

    def test_new_resources_enter_search_immediately(self):
        self.c.register_batch(
            [
                {"type": "schema", "name": "Findable", "version": "1.0",
                 "document": {"type": "object",
                              "properties": {"alpha": {"type": "string"}}}},
                {"type": "asset", "asset_id": "svc-find", "name": "查找服务",
                 "kind": "service",
                 "refs": [{"schema": "Addr", "version": "1.0", "path": "/street"}]},
            ]
        )
        schema_hits = self.c.search("findable", doc_type="schema")
        self.assertEqual(len(schema_hits), 1)
        self.assertEqual(schema_hits[0]["name"], "Findable")
        asset_hits = self.c.search(doc_type="asset", schema="Addr")
        self.assertEqual({h["id"] for h in asset_hits}, {"svc-find"})
        page = self.c.search_page(doc_type="schema", page_size=10)
        self.assertGreaterEqual(page["total"], 2)
        facets = self.c.search_facets(doc_type="asset")
        self.assertEqual(facets["total"], 1)

    def test_deep_copy_immutability(self):
        document = {"type": "object", "properties": {"m": {"type": "string"}}}
        view = self.c.register_batch(
            [{"type": "schema", "name": "Imm", "version": "1.0",
              "document": document}]
        )["created"][0]
        document["properties"]["hacked"] = {"type": "integer"}
        view["document"]["properties"]["view_hack"] = {"type": "boolean"}
        # 内部存储既不与输入共享，也不与返回视图共享。
        stored = self.c.get_schema("Imm", "1.0")
        self.assertEqual(set(stored["document"]["properties"]), {"m"})

        refs_entry = {"schema": "Addr", "version": "1.0", "path": "/street"}
        asset_view = self.c.register_batch(
            [{"type": "asset", "asset_id": "svc-imm", "name": "n", "kind": "k",
              "refs": [refs_entry]}]
        )["created"][0]
        refs_entry["path"] = "/hacked"
        asset_view["refs"].append({"schema": "X", "version": "1", "path": "/x"})
        again = self.c.get_asset("svc-imm")
        self.assertEqual(
            again["refs"],
            [{"schema": "Addr", "version": "1.0", "path": "/street"}],
        )

    def test_limit_boundary_allowed(self):
        resources = [
            {"type": "schema", "name": f"B{i}", "version": "1",
             "document": {"type": "object"}}
            for i in range(limits.MAX_BATCH_RESOURCES)
        ]
        result = self.c.register_batch(resources)
        self.assertEqual(len(result["created"]), limits.MAX_BATCH_RESOURCES)


class RegisterBatchInvalidTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()

    def _assert_invalid(self, resources):
        with self.assertRaises(BatchRegistrationInvalid) as cm:
            self.c.register_batch(resources)
        return cm.exception

    def test_resources_not_list(self):
        err = self._assert_invalid({"type": "schema"})
        self.assertEqual(err.details["reason"], "resources_not_list")

    def test_entry_not_object(self):
        err = self._assert_invalid(["x"])
        self.assertEqual(err.details["reason"], "entry_not_object")
        self.assertEqual(err.details["index"], 0)

    def test_invalid_type(self):
        err = self._assert_invalid([{"type": "table"}])
        self.assertEqual(err.details["reason"], "invalid_type")

    def test_missing_type(self):
        err = self._assert_invalid([{"name": "S", "version": "1"}])
        self.assertEqual(err.details["reason"], "invalid_type")

    def test_schema_bad_name_and_version(self):
        for value, reason in [
            ({"type": "schema", "version": "1"}, "invalid_name"),
            ({"type": "schema", "name": "S", "version": ""}, "invalid_version"),
            ({"type": "schema", "name": "bad/name", "version": "1"},
             "invalid_name"),
            ({"type": "schema", "name": 3, "version": "1"}, "invalid_name"),
        ]:
            err = self._assert_invalid([value])
            self.assertEqual(err.details["reason"], reason, value)

    def test_schema_missing_document(self):
        err = self._assert_invalid(
            [{"type": "schema", "name": "S", "version": "1"}]
        )
        self.assertEqual(err.details["reason"], "missing_document")

    def test_duplicate_schema_in_batch(self):
        err = self._assert_invalid(
            [
                {"type": "schema", "name": "S", "version": "1",
                 "document": {"type": "object"}},
                {"type": "schema", "name": "S", "version": "1",
                 "document": {"type": "integer"}},
            ]
        )
        self.assertEqual(err.details["reason"], "duplicate_schema")
        self.assertEqual(err.details["index"], 1)

    def test_asset_bad_fields(self):
        base = {"type": "asset", "asset_id": "a1", "name": "n", "kind": "k"}
        for patch, reason in [
            ({"asset_id": ""}, "invalid_asset_id"),
            ({"asset_id": "bad id"}, "invalid_asset_id"),
            ({"asset_id": 7}, "invalid_asset_id"),
            ({"name": ""}, "invalid_asset_name"),
            ({"name": 1}, "invalid_asset_name"),
            ({"kind": ""}, "invalid_asset_kind"),
            ({"kind": None}, "invalid_asset_kind"),
            ({"refs": {}}, "invalid_refs"),
        ]:
            value = {**base, **patch}
            err = self._assert_invalid([value])
            self.assertEqual(err.details["reason"], reason, patch)

    def test_duplicate_asset_in_batch(self):
        err = self._assert_invalid(
            [
                {"type": "asset", "asset_id": "a1", "name": "n", "kind": "k"},
                {"type": "asset", "asset_id": "a1", "name": "n2", "kind": "k2"},
            ]
        )
        self.assertEqual(err.details["reason"], "duplicate_asset")

    def test_refs_bad_structure(self):
        base = {"type": "asset", "asset_id": "a1", "name": "n", "kind": "k"}
        for refs, reason in [
            (["x"], "ref_not_object"),
            ([{"version": "1", "path": ""}], "invalid_ref_schema"),
            ([{"schema": "S", "version": 1}], "invalid_ref_version"),
            ([{"schema": "S", "version": "1", "path": "rel"}],
             "invalid_pointer"),
            ([{"schema": "S", "version": "1", "path": "/a~2"}],
             "invalid_pointer"),
            ([{"schema": "S", "version": "1", "path": 3}],
             "invalid_ref_path"),
        ]:
            err = self._assert_invalid([{**base, "refs": refs}])
            self.assertEqual(err.details["reason"], reason, refs)

    def test_too_large(self):
        resources = [
            {"type": "schema", "name": f"S{i}", "version": "1",
             "document": {"type": "object"}}
            for i in range(limits.MAX_BATCH_RESOURCES + 1)
        ]
        with self.assertRaises(BatchRegistrationTooLarge) as cm:
            self.c.register_batch(resources)
        self.assertEqual(cm.exception.details["reason"], "batch_too_large")
        self.assertEqual(cm.exception.details["limit"], limits.MAX_BATCH_RESOURCES)
        self.assertEqual(cm.exception.details["size"],
                         limits.MAX_BATCH_RESOURCES + 1)


class RegisterBatchSemanticErrorTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema("Addr", "1.0", ADDR_DOC)

    def test_invalid_document_raises_comparison_invalid(self):
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.register_batch(
                [{"type": "schema", "name": "Bad", "version": "1",
                  "document": "not-a-schema"}]
            )
        # 文档内非法关键字同样是文档合法性问题。
        with self.assertRaises(SchemaComparisonInvalid):
            self.c.register_batch(
                [{"type": "schema", "name": "Bad", "version": "1",
                  "document": {"type": "wat"}}]
            )
        # 结构错误优先于文档错误。
        with self.assertRaises(BatchRegistrationInvalid):
            self.c.register_batch(
                [{"type": "schema", "name": "Bad", "document": {}}]
            )

    def test_conflict_with_existing_schema(self):
        with self.assertRaises(AlreadyExistsError) as cm:
            self.c.register_batch(
                [{"type": "schema", "name": "Addr", "version": "1.0",
                  "document": {"type": "object"}}]
            )
        self.assertEqual(cm.exception.details,
                         {"schema": "Addr", "version": "1.0"})

    def test_conflict_with_existing_asset(self):
        self.c.register_asset("svc-1", "n", "k")
        with self.assertRaises(AlreadyExistsError) as cm:
            self.c.register_batch(
                [{"type": "asset", "asset_id": "svc-1", "name": "n2",
                  "kind": "k2"}]
            )
        self.assertEqual(cm.exception.details, {"asset": "svc-1"})

    def test_ref_field_missing_in_batch_target(self):
        with self.assertRaises(NotFoundError):
            self.c.register_batch(
                [
                    {"type": "schema", "name": "X", "version": "1",
                     "document": {"type": "object", "properties": {
                         "a": {"$ref": "Addr@1.0#/properties/nope"}}}},
                ]
            )

    def test_asset_ref_field_missing(self):
        with self.assertRaises(NotFoundError):
            self.c.register_batch(
                [{"type": "asset", "asset_id": "svc-z", "name": "n",
                  "kind": "k",
                  "refs": [{"schema": "Addr", "version": "1.0",
                            "path": "/missing"}]}]
            )

    def test_asset_ref_unknown_schema(self):
        with self.assertRaises(NotFoundError) as cm:
            self.c.register_batch(
                [{"type": "asset", "asset_id": "svc-z", "name": "n",
                  "kind": "k",
                  "refs": [{"schema": "Ghost", "version": "9", "path": ""}]}]
            )
        self.assertEqual(cm.exception.details,
                         {"schema": "Ghost", "version": "9"})

    def test_conflict_precedes_not_found(self):
        # 同一批次既含既有冲突又含引用不存在：冲突（AlreadyExistsError）优先。
        self.c.register_asset("svc-exists", "n", "k")
        with self.assertRaises(AlreadyExistsError):
            self.c.register_batch(
                [
                    {"type": "asset", "asset_id": "svc-exists", "name": "n",
                     "kind": "k"},
                    {"type": "asset", "asset_id": "svc-other", "name": "n",
                     "kind": "k",
                     "refs": [{"schema": "Ghost", "version": "1", "path": ""}]},
                ]
            )


class RegisterBatchAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema("Addr", "1.0", ADDR_DOC)
        self.c.register_asset(
            "svc-old", "旧服务", "service",
            [{"schema": "Addr", "version": "1.0", "path": "/street"}],
        )

    def _state_snapshot(self):
        return {
            "schemas": {
                (n, v): self.c.get_schema(n, v)
                for n in ("Addr", "Person", "Company", "X")
                for v in (self.c.list_versions(n) if self._has(n) else [])
            },
            "assets": self.c.search(doc_type="asset"),
            "search": self.c.search(),
            "facets": self.c.search_facets(),
        }

    def _has(self, name):
        try:
            self.c.list_versions(name)
            return True
        except NotFoundError:
            return False

    def test_failure_leaves_no_partial_effect(self):
        before = self._state_snapshot()
        failing = [
            {"type": "schema", "name": "Person", "version": "1.0",
             "document": person_doc()},
            {"type": "schema", "name": "Company", "version": "1.0",
             "document": {"type": "object",
                          "properties": {"ceo": {"type": "string"}}}},
            {"type": "asset", "asset_id": "svc-new", "name": "新服务",
             "kind": "service",
             "refs": [{"schema": "Person", "version": "1.0", "path": "/home"}]},
            # 最后一项非法：引用本批不存在的字段。
            {"type": "asset", "asset_id": "svc-bad", "name": "坏服务",
             "kind": "service",
             "refs": [{"schema": "Company", "version": "1.0",
                       "path": "/nope"}]},
        ]
        with self.assertRaises(NotFoundError):
            self.c.register_batch(failing)

        with self.assertRaises(NotFoundError):
            self.c.get_schema("Person", "1.0")
        with self.assertRaises(NotFoundError):
            self.c.get_asset("svc-new")
        self.assertFalse(self._has("Person"))
        self.assertFalse(self._has("Company"))
        after = self._state_snapshot()
        self.assertEqual(after["search"], before["search"])
        self.assertEqual(after["facets"], before["facets"])
        self.assertEqual({a["id"] for a in after["assets"]}, {"svc-old"})

    def test_failure_keeps_reports_and_pages(self):
        # 先生成一份比较报告。
        self.c.register_schema("Person", "1.0", {"type": "object", "properties": {
            "name": {"type": "string"}}})
        self.c.compare_schemas(
            "Person", "1.0",
            {"type": "object", "properties": {
                "name": {"type": "string"}, "age": {"type": "integer"}}},
        )
        reports_before = self.c.list_reports()
        page_before = self.c.search_page(page_size=2)

        with self.assertRaises(BatchRegistrationInvalid):
            self.c.register_batch(
                [{"type": "schema", "name": "Person", "version": "2.0"},
                 {"type": "weird"}]
            )
        self.assertEqual(self.c.list_reports(), reports_before)
        self.assertEqual(self.c.search_page(page_size=2), page_before)

    def test_invalid_batch_second_attempt_can_succeed(self):
        failing = [
            {"type": "asset", "asset_id": "svc-a", "name": "a", "kind": "k",
             "refs": [{"schema": "Ghost", "version": "1", "path": ""}]},
            {"type": "schema", "name": "Ok", "version": "1",
             "document": {"type": "object"}},
        ]
        with self.assertRaises(NotFoundError):
            self.c.register_batch(copy.deepcopy(failing))
        with self.assertRaises(NotFoundError):
            self.c.get_schema("Ok", "1")
        # 修正后整批成功。
        fixed = copy.deepcopy(failing)
        fixed[0]["refs"] = [{"schema": "Addr", "version": "1.0", "path": ""}]
        result = self.c.register_batch(fixed)
        self.assertEqual(
            [x["type"] for x in result["created"]],
            ["asset", "schema"],
        )
        self.assertEqual(self.c.get_asset("svc-a")["kind"], "k")


if __name__ == "__main__":
    unittest.main()
