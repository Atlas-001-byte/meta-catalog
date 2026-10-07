"""MetaCatalog.register_batch 原子批量登记测试。"""

import unittest

from meta_catalog import MetaCatalog
from meta_catalog.errors import (
    AlreadyExistsError,
    BatchRegistrationInvalid,
    BatchRegistrationTooLarge,
    NotFoundError,
    SchemaComparisonInvalid,
)
from meta_catalog.limits import MAX_BATCH_RESOURCES


def obj(**props):
    return {"type": "object", "properties": props}


def schema_entry(name="S", version="1.0", document=None, **extra):
    return {
        "type": "schema",
        "name": name,
        "version": version,
        "document": obj(x={"type": "string"}) if document is None else document,
        **extra,
    }


def asset_entry(asset_id="svc1", name="服务", kind="service", refs=None, **extra):
    return {
        "type": "asset",
        "asset_id": asset_id,
        "name": name,
        "kind": kind,
        "refs": [] if refs is None else refs,
        **extra,
    }


class RegisterBatchSuccessTests(unittest.TestCase):
    # ------------------------------------------------------------- 成功返回
    def test_empty_batch_returns_empty_created(self):
        c = MetaCatalog()
        self.assertEqual(c.register_batch([]), {"created": []})

    def test_created_in_input_order_with_typed_views(self):
        c = MetaCatalog()
        res = c.register_batch(
            [
                schema_entry("Addr", "1.0", obj(street={"type": "string"})),
                asset_entry(
                    "svc-addr",
                    "地址服务",
                    "service",
                    refs=[{"schema": "Addr", "version": "1.0", "path": "/street"}],
                ),
                schema_entry(
                    "Person",
                    "1.0",
                    obj(home={"$ref": "Addr@1.0#/properties/street"}),
                ),
            ]
        )
        self.assertEqual(list(res), ["created"])
        self.assertEqual(len(res["created"]), 3)
        self.assertEqual(
            [item["type"] for item in res["created"]],
            ["schema", "asset", "schema"],
        )
        # Schema 视图与 get_schema 同构。
        view = res["created"][0]
        self.assertEqual(
            view,
            {
                "type": "schema",
                "name": "Addr",
                "version": "1.0",
                "title": None,
                "document": obj(street={"type": "string"}),
            },
        )
        self.assertEqual(view, {"type": "schema", **c.get_schema("Addr", "1.0")})
        # 资产视图与 get_asset 同构，refs 已规整为逻辑路径。
        av = res["created"][1]
        self.assertEqual(
            av,
            {
                "type": "asset",
                "asset_id": "svc-addr",
                "name": "地址服务",
                "kind": "service",
                "refs": [{"schema": "Addr", "version": "1.0", "path": "/street"}],
            },
        )
        self.assertEqual(av, {"type": "asset", **c.get_asset("svc-addr")})

    def test_same_name_new_versions_append_in_input_order(self):
        c = MetaCatalog()
        c.register_batch(
            [
                schema_entry("V", "2.0", True),
                schema_entry("V", "1.0", True),
            ]
        )
        self.assertEqual(c.list_versions("V"), ["2.0", "1.0"])
        c.register_batch([schema_entry("V", "3.0", True)])
        self.assertEqual(c.list_versions("V"), ["2.0", "1.0", "3.0"])

    def test_refs_accept_document_pointer_and_normalize(self):
        c = MetaCatalog()
        res = c.register_batch(
            [
                schema_entry("Addr", "1.0", obj(street={"type": "string"})),
                asset_entry(
                    "svc",
                    "n",
                    "service",
                    refs=[
                        {"schema": "Addr", "version": "1.0", "path": "/properties/street"},
                        {"schema": "Addr", "version": "1.0", "path": "/street"},
                    ],
                ),
            ]
        )
        self.assertEqual(
            res["created"][1]["refs"],
            [{"schema": "Addr", "version": "1.0", "path": "/street"}],
        )

    # ------------------------------------------------------- 本批引用解析
    def test_asset_refs_resolve_against_batch_schemas(self):
        c = MetaCatalog()
        res = c.register_batch(
            [
                asset_entry(
                    "svc",
                    "n",
                    "service",
                    refs=[{"schema": "Late", "version": "1.0", "path": "/x"}],
                ),
                schema_entry("Late", "1.0", obj(x={"type": "string"})),
            ]
        )
        self.assertEqual(
            res["created"][0]["refs"],
            [{"schema": "Late", "version": "1.0", "path": "/x"}],
        )

    def test_forward_external_ref_and_cycle_allowed(self):
        c = MetaCatalog()
        c.register_batch(
            [
                schema_entry(
                    "A",
                    "1.0",
                    obj(x={"$ref": "B@1.0#"}),
                ),
                schema_entry(
                    "B",
                    "1.0",
                    obj(y={"$ref": "A@1.0#"}),
                ),
            ]
        )
        # 前向指向尚不存在的版本：仍然允许。
        c.register_batch(
            [schema_entry("Fwd", "1.0", obj(z={"$ref": "Ghost@9.9#/properties/q"}))]
        )
        self.assertTrue(c.list_versions("Fwd"), ["1.0"])
        # 引用环不影响后续影响分析终止性。
        imp = c.analyze_impact("A", "1.0", "/x")
        self.assertEqual(imp["direct_assets"], [])
        imp2 = c.analyze_impact("B", "1.0", "/y")
        self.assertEqual(imp2["transitive_assets"], [])

    def test_batch_cross_refs_feed_transitive_impact(self):
        c = MetaCatalog()
        c.register_batch(
            [
                schema_entry("Addr", "1.0", obj(street={"type": "string"})),
                schema_entry(
                    "Person",
                    "1.0",
                    obj(home={"$ref": "Addr@1.0#/properties/street"}),
                ),
                asset_entry(
                    "svc",
                    "邮寄",
                    "service",
                    refs=[{"schema": "Person", "version": "1.0", "path": "/home"}],
                ),
            ]
        )
        imp = c.analyze_impact("Addr", "1.0", "/street")
        self.assertEqual(
            [(a["asset_id"], a["path"]) for a in imp["transitive_assets"]],
            [("svc", "/home")],
        )

    # ------------------------------------------------------------- 检索
    def test_new_resources_enter_search_immediately(self):
        c = MetaCatalog()
        c.register_batch(
            [
                schema_entry("PingDoc", "1.0", obj(keywordfield={"type": "string"})),
                asset_entry(
                    "ping-asset",
                    "PingAsset",
                    "service",
                    refs=[{"schema": "PingDoc", "version": "1.0", "path": "/keywordfield"}],
                ),
            ]
        )
        schemas = c.search("pingdoc", doc_type="schema")
        self.assertEqual([d["name"] for d in schemas], ["PingDoc"])
        assets = c.search("pingasset", doc_type="asset")
        self.assertEqual([d["id"] for d in assets], ["ping-asset"])

    def test_search_ranking_order_independent_of_registration(self):
        c = MetaCatalog()
        c.register_batch(
            [
                schema_entry("Zzz", "1.0", obj(common={"type": "string"})),
                schema_entry("Aaa", "1.0", obj(common={"type": "string"})),
            ]
        )
        names = [d["name"] for d in c.search(doc_type="schema")]
        self.assertEqual(names, ["Aaa", "Zzz"])

    # ----------------------------------------------------------- 深拷贝
    def test_immutable_deep_copy_on_store_and_return(self):
        c = MetaCatalog()
        doc = obj(m={"type": "string"})
        res = c.register_batch([schema_entry("Mut", "1.0", doc)])
        doc["properties"]["m"]["type"] = "integer"
        doc["properties"]["hacked"] = True
        self.assertEqual(
            c.get_schema("Mut", "1.0")["document"],
            obj(m={"type": "string"}),
        )
        res["created"][0]["document"]["x"] = True
        self.assertNotIn("x", c.get_schema("Mut", "1.0")["document"])

    def test_asset_input_not_retained_by_reference(self):
        c = MetaCatalog()
        entry = asset_entry(
            "svc",
            "n",
            "service",
            refs=[{"schema": "S", "version": "1.0", "path": "/x"}],
        )
        c.register_batch(
            [
                schema_entry("S", "1.0", obj(x={"type": "string"})),
                entry,
            ]
        )
        entry["name"] = "changed"
        entry["refs"].append({"schema": "S", "version": "1.0", "path": "/x"})
        self.assertEqual(c.get_asset("svc")["name"], "n")

    def test_bool_schema_documents_supported(self):
        c = MetaCatalog()
        res = c.register_batch(
            [
                schema_entry("T", "1.0", True),
                schema_entry("F", "1.0", False),
            ]
        )
        self.assertEqual([d["name"] for d in res["created"]], ["T", "F"])


class RegisterBatchInvalidTests(unittest.TestCase):
    # ------------------------------------------------------------- 结构校验
    def test_resources_must_be_list(self):
        c = MetaCatalog()
        with self.assertRaises(BatchRegistrationInvalid) as ctx:
            c.register_batch({"type": "schema"})
        self.assertEqual(ctx.exception.details["reason"], "resources_not_list")

    def test_entry_must_be_mapping(self):
        c = MetaCatalog()
        with self.assertRaises(BatchRegistrationInvalid) as ctx:
            c.register_batch(["nope"])
        self.assertEqual(ctx.exception.details["reason"], "entry_not_mapping")
        self.assertEqual(ctx.exception.details["index"], 0)

    def test_type_required_and_allowed(self):
        c = MetaCatalog()
        for bad in (None, "Schema", 1, ""):
            with self.assertRaises(BatchRegistrationInvalid) as ctx:
                c.register_batch([{"type": bad}])
            self.assertEqual(ctx.exception.details["reason"], "invalid_type")

    def test_schema_required_fields(self):
        c = MetaCatalog()
        cases = [
            ({"type": "schema", "version": "1.0", "document": True}, "invalid_name"),
            ({"type": "schema", "name": "S", "document": True}, "invalid_version"),
            ({"type": "schema", "name": "S", "version": "1.0"}, "missing_document"),
            (
                {"type": "schema", "name": 1, "version": "1.0", "document": True},
                "invalid_name",
            ),
            (
                {"type": "schema", "name": "S", "version": 2, "document": True},
                "invalid_version",
            ),
        ]
        for entry, reason in cases:
            with self.assertRaises(BatchRegistrationInvalid) as ctx:
                c.register_batch([entry])
            self.assertEqual(ctx.exception.details["reason"], reason, entry)

    def test_names_and_versions_pattern(self):
        c = MetaCatalog()
        for name, version in [("bad name", "1.0"), ("S", "v 1"), ("S", "")]:
            with self.assertRaises(BatchRegistrationInvalid):
                c.register_batch(
                    [{"type": "schema", "name": name, "version": version, "document": True}]
                )

    def test_asset_required_fields_and_types(self):
        c = MetaCatalog()
        base = {"type": "asset", "asset_id": "a1", "name": "n", "kind": "k"}
        cases = [
            ({**base, "asset_id": None}, "invalid_asset_id"),
            ({**base, "asset_id": "bad id"}, "invalid_asset_id"),
            ({**base, "name": ""}, "invalid_name"),
            ({**base, "name": 7}, "invalid_name"),
            ({**base, "kind": ""}, "invalid_kind"),
            ({**base, "kind": 7}, "invalid_kind"),
            ({**base, "refs": {}}, "invalid_refs"),
            ({**base, "refs": ["x"]}, "invalid_ref"),
            ({**base, "refs": [{"schema": 1, "version": "1.0"}]}, "invalid_ref"),
            ({**base, "refs": [{"schema": "S", "version": 1}]}, "invalid_ref"),
            (
                {**base, "refs": [{"schema": "S", "version": "1.0", "path": 1}]},
                "invalid_ref",
            ),
            (
                {**base, "refs": [{"schema": "S", "version": "1.0", "path": "bad"}]},
                "invalid_pointer",
            ),
        ]
        for entry, reason in cases:
            with self.assertRaises(BatchRegistrationInvalid) as ctx:
                c.register_batch([entry])
            self.assertEqual(ctx.exception.details["reason"], reason, entry)

    def test_root_pointer_is_valid(self):
        c = MetaCatalog()
        res = c.register_batch(
            [
                schema_entry("S", "1.0", obj(x={"type": "string"})),
                asset_entry(
                    "a",
                    "n",
                    "k",
                    refs=[{"schema": "S", "version": "1.0", "path": ""}],
                ),
            ]
        )
        self.assertEqual(
            res["created"][1]["refs"],
            [{"schema": "S", "version": "1.0", "path": ""}],
        )

    def test_batch_duplicate_schema_and_asset(self):
        c = MetaCatalog()
        with self.assertRaises(BatchRegistrationInvalid) as ctx:
            c.register_batch(
                [
                    schema_entry("D", "1.0", True),
                    schema_entry("D", "1.0", False),
                ]
            )
        self.assertEqual(ctx.exception.details["reason"], "duplicate_schema")
        with self.assertRaises(BatchRegistrationInvalid) as ctx:
            c.register_batch(
                [
                    asset_entry("dup", "n1", "k"),
                    asset_entry("dup", "n2", "k"),
                ]
            )
        self.assertEqual(ctx.exception.details["reason"], "duplicate_asset")

    def test_too_large(self):
        c = MetaCatalog()
        items = [asset_entry(f"a{i}", "n", "k") for i in range(MAX_BATCH_RESOURCES + 1)]
        with self.assertRaises(BatchRegistrationTooLarge) as ctx:
            c.register_batch(items)
        self.assertEqual(ctx.exception.details["limit"], MAX_BATCH_RESOURCES)

    def test_size_at_limit_allowed(self):
        c = MetaCatalog()
        items = [
            schema_entry("Only", "1.0", True)
        ] + [asset_entry(f"a{i}", "n", "k") for i in range(MAX_BATCH_RESOURCES - 1)]
        res = c.register_batch(items)
        self.assertEqual(len(res["created"]), MAX_BATCH_RESOURCES)

    # ----------------------------------------------------------- 文档不合法
    def test_invalid_schema_document(self):
        c = MetaCatalog()
        with self.assertRaises(SchemaComparisonInvalid):
            c.register_batch(
                [schema_entry("S", "1.0", {"type": "not-a-type"})]
            )

    def test_invalid_document_takes_precedence_over_conflict(self):
        c = MetaCatalog()
        c.register_schema("S", "1.0", True)
        with self.assertRaises(SchemaComparisonInvalid):
            c.register_batch(
                [schema_entry("S", "1.0", {"type": "bad"})]
            )

    # --------------------------------------------------------------- 冲突
    def test_conflict_with_existing_schema(self):
        c = MetaCatalog()
        c.register_schema("S", "1.0", True)
        with self.assertRaises(AlreadyExistsError):
            c.register_batch([schema_entry("S", "1.0", True)])

    def test_conflict_with_existing_asset(self):
        c = MetaCatalog()
        c.register_asset("a1", "n", "k")
        with self.assertRaises(AlreadyExistsError):
            c.register_batch([asset_entry("a1", "n2", "k2")])

    # ------------------------------------------------------ 引用字段不存在
    def test_asset_ref_missing_field_raises_not_found(self):
        c = MetaCatalog()
        c.register_schema("S", "1.0", obj(x={"type": "string"}))
        with self.assertRaises(NotFoundError):
            c.register_batch(
                [asset_entry("a", "n", "k", refs=[{"schema": "S", "version": "1.0", "path": "/y"}])]
            )

    def test_asset_ref_missing_schema_version_raises_not_found(self):
        c = MetaCatalog()
        with self.assertRaises(NotFoundError):
            c.register_batch(
                [asset_entry("a", "n", "k", refs=[{"schema": "S", "version": "1.0", "path": "/y"}])]
            )

    def test_cross_ref_existing_target_field_missing_fails_whole_batch(self):
        c = MetaCatalog()
        c.register_schema("X", "1.0", obj(a={"type": "string"}))
        with self.assertRaises(NotFoundError):
            c.register_batch(
                [
                    schema_entry(
                        "Y",
                        "1.0",
                        obj(z={"$ref": "X@1.0#/properties/nope"}),
                    )
                ]
            )

    def test_cross_ref_batch_target_field_missing_fails_whole_batch(self):
        c = MetaCatalog()
        with self.assertRaises(NotFoundError):
            c.register_batch(
                [
                    schema_entry(
                        "Y",
                        "1.0",
                        obj(z={"$ref": "Z@1.0#/properties/nope"}),
                    ),
                    schema_entry("Z", "1.0", obj(q={"type": "string"})),
                ]
            )

    # ------------------------------------------------------------- 原子性
    def test_failure_leaves_no_partial_registration(self):
        c = MetaCatalog()
        with self.assertRaises(NotFoundError):
            c.register_batch(
                [
                    schema_entry("Ok", "1.0", obj(p={"type": "string"})),
                    asset_entry(
                        "bad",
                        "n",
                        "k",
                        refs=[{"schema": "Ok", "version": "1.0", "path": "/missing"}],
                    ),
                ]
            )
        with self.assertRaises(NotFoundError):
            c.get_schema("Ok", "1.0")
        with self.assertRaises(NotFoundError):
            c.get_asset("bad")
        with self.assertRaises(NotFoundError):
            c.list_versions("Ok")
        self.assertEqual(c.search(), [])

    def test_failure_does_not_change_existing_reads(self):
        c = MetaCatalog()
        c.register_schema("Keep", "1.0", obj(v={"type": "string"}))
        before = {
            "schema": c.get_schema("Keep", "1.0"),
            "versions": c.list_versions("Keep"),
            "search": c.search(),
            "facets": c.search_facets(),
            "page": c.search_page(),
        }
        with self.assertRaises(AlreadyExistsError):
            c.register_batch(
                [
                    schema_entry("Keep", "1.0", True),
                    schema_entry("Nope", "1.0", True),
                ]
            )
        self.assertEqual(c.get_schema("Keep", "1.0"), before["schema"])
        self.assertEqual(c.list_versions("Keep"), before["versions"])
        self.assertEqual(c.search(), before["search"])
        self.assertEqual(c.search_facets(), before["facets"])
        self.assertEqual(c.search_page(), before["page"])

    def test_retry_after_failure_succeeds(self):
        c = MetaCatalog()
        with self.assertRaises(NotFoundError):
            c.register_batch(
                [
                    schema_entry("Ok", "1.0", obj(p={"type": "string"})),
                    asset_entry(
                        "bad",
                        "n",
                        "k",
                        refs=[{"schema": "Ok", "version": "1.0", "path": "/missing"}],
                    ),
                ]
            )
        res = c.register_batch(
            [
                schema_entry("Ok", "1.0", obj(p={"type": "string"})),
                asset_entry(
                    "good",
                    "n",
                    "k",
                    refs=[{"schema": "Ok", "version": "1.0", "path": "/p"}],
                ),
            ]
        )
        self.assertEqual([v["type"] for v in res["created"]], ["schema", "asset"])


class RegisterBatchMixedOrderTests(unittest.TestCase):
    def test_assets_before_and_after_schemas(self):
        c = MetaCatalog()
        res = c.register_batch(
            [
                asset_entry(
                    "a0",
                    "n0",
                    "k",
                    refs=[{"schema": "S", "version": "1.0", "path": ""}],
                ),
                schema_entry("S", "1.0", obj(x={"type": "string"})),
                asset_entry(
                    "a1",
                    "n1",
                    "k",
                    refs=[{"schema": "S", "version": "1.0", "path": "/x"}],
                ),
            ]
        )
        self.assertEqual([r["asset_id"] for r in res["created"] if r["type"] == "asset"],
                         ["a0", "a1"])
        self.assertEqual(
            res["created"][0]["refs"],
            [{"schema": "S", "version": "1.0", "path": ""}],
        )

    def test_largeish_batch_stable(self):
        # 20 个 Schema + 20 个资产，全部指向首个 Schema 根。
        c = MetaCatalog()
        resources = [schema_entry("Base", "1.0", obj(x={"type": "string"}))]
        for i in range(20):
            resources.append(schema_entry(f"S{i}", "1.0", True))
        for i in range(20):
            resources.append(
                asset_entry(
                    f"a{i}",
                    f"name{i}",
                    "service",
                    refs=[{"schema": "Base", "version": "1.0", "path": "/x"}],
                )
            )
        res = c.register_batch(resources)
        self.assertEqual(len(res["created"]), 41)
        self.assertEqual(len(c.search(doc_type="asset")), 20)


if __name__ == "__main__":
    unittest.main()
