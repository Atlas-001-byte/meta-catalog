import json
import unittest

from meta_catalog import MetaCatalog
from meta_catalog.errors import NotFoundError


def obj(**props):
    return {"type": "object", "properties": props}


def status_map(report):
    return {(r["source_path"], r["target_schema"], r["target_path"]):
            r["status"] for r in report["references"]}


class AuditStatusTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()

    def _register_person_address(self):
        # 源先于目标注册（前向引用），各类非法目标在审计期才暴露。
        self.c.register_schema("Person", "1.0", obj(
            name={"type": "string"},
            addr={"$ref": "Address@1.0#/properties/street"},
            root={"$ref": "Address@1.0#"},
            ghost={"$ref": "Gone@1.0#/properties/g"},
            nofield={"$ref": "Address@1.0#/properties/nope"},
            badseg={"$ref": "Address@1.0#/required/0"},
            bareprops={"$ref": "Address@1.0#/properties"},
        ))
        self.c.register_schema("Address", "1.0", {
            "type": "object",
            "properties": {"street": {"type": "string"}},
            "required": ["street"],
        })

    def test_status_classification(self):
        self._register_person_address()
        report = self.c.check_schema_references("Person", "1.0")
        sm = status_map(report)
        self.assertEqual(sm[("/addr", "Address", "/street")], "resolved")
        self.assertEqual(sm[("/root", "Address", "")], "resolved")
        self.assertEqual(sm[("/ghost", "Gone", "/g")], "missing_schema")
        self.assertEqual(sm[("/nofield", "Address", "/nope")], "missing_field")
        self.assertEqual(sm[("/badseg", "Address", "/required/0")], "invalid_pointer")
        self.assertEqual(sm[("/bareprops", "Address", "")], "invalid_pointer")

    def test_counts_and_resolved_includes_refless_sources(self):
        self._register_person_address()
        self.c.register_schema("Lone", "1.0", obj(z={"type": "string"}))
        report = self.c.check_schema_references()
        self.assertEqual(report["checked"], 3)
        self.assertEqual(report["total"], 3)
        # Address 无外部引用（计入 resolved），Lone 无引用（计入 resolved），
        # Person 含未解析引用（不计入）。
        self.assertEqual(report["resolved"], 2)

    def test_no_issues_when_all_resolved(self):
        c = MetaCatalog()
        c.register_schema("Address", "1.0", obj(street={"type": "string"}))
        c.register_schema("Person", "1.0", obj(
            addr={"$ref": "Address@1.0#/properties/street"}
        ))
        report = c.check_schema_references()
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["resolved"], report["total"])
        self.assertEqual(report["checked"], 2)

    def test_resolved_through_reference_chain(self):
        # C:/x -> B:/b/... -> A:/x 子树；跨边可达应判 resolved / missing_field。
        self.c.register_schema("C", "1.0", obj(
            good={"$ref": "B@1.0#/properties/b/properties/p"},
            deep={"$ref": "B@1.0#/properties/b/properties/zzz"},
        ))
        self.c.register_schema("B", "1.0", obj(
            b={"$ref": "A@1.0#/properties/x"}
        ))
        self.c.register_schema("A", "1.0", obj(
            x=obj(p={"type": "string"})
        ))
        sm = status_map(self.c.check_schema_references("C", "1.0"))
        self.assertEqual(sm[("/good", "B", "/b/p")], "resolved")
        self.assertEqual(sm[("/deep", "B", "/b/zzz")], "missing_field")

    def test_internal_refs_not_audited(self):
        c = MetaCatalog()
        c.register_schema("Person", "1.0", {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/Name"}},
            "$defs": {"Name": {"type": "string"}},
        })
        report = c.check_schema_references()
        self.assertEqual(report["references"], [])
        self.assertEqual(report["resolved"], 1)

    def test_cross_schema_cycle_terminates_and_resolves(self):
        c = MetaCatalog()
        c.register_schema("A", "1.0", obj(
            x={"type": "string"}, b={"$ref": "B@1.0#"}
        ))
        c.register_schema("B", "1.0", obj(
            y={"type": "string"}, a={"$ref": "A@1.0#"}
        ))
        report = c.check_schema_references()  # 必须终止
        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["resolved"], 2)
        self.assertEqual(report["issues"], [])
        targets = {(r["source_schema"], r["target_schema"]) for r in report["references"]}
        self.assertEqual(targets, {("A", "B"), ("B", "A")})

    def test_logical_path_conventions_root_dash_star(self):        # 数组元素 - 与 additionalProperties * 口径沿用影响分析。
        self.c.register_schema("T", "1.0", {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"$ref": "Addr2@1.0#"}},
                "map": {"type": "object",
                        "additionalProperties": {"$ref": "Addr2@1.0#"}},
            },
        })
        self.c.register_schema("Addr2", "1.0", {"type": "string"})
        paths = {r["source_path"] for r in self.c.check_schema_references("T", "1.0")["references"]}
        self.assertIn("/tags/-", paths)
        self.assertIn("/map/*", paths)

    def test_bool_schema_root_and_array_invalid_pointer(self):
        c = MetaCatalog()
        c.register_schema("Use", "1.0", obj(
            flag={"$ref": "Flag@1.0#"},
            idx_bad={"$ref": "Tup@1.0#/prefixItems/9"},
        ))
        c.register_schema("Flag", "1.0", True)
        c.register_schema("Tup", "1.0", {"type": "array", "prefixItems": [{"type": "string"}]})
        sm = status_map(c.check_schema_references("Use", "1.0"))
        self.assertEqual(sm[("/flag", "Flag", "")], "resolved")
        self.assertEqual(sm[("/idx_bad", "Tup", "/9")], "invalid_pointer")

    def test_reference_field_keys(self):
        c = MetaCatalog()
        c.register_schema("Address", "1.0", obj(street={"type": "string"}))
        c.register_schema("Person", "1.0", obj(
            addr={"$ref": "Address@1.0#/properties/street"}
        ))
        ref = c.check_schema_references("Person", "1.0")["references"][0]
        self.assertEqual(set(ref), {
            "source_schema", "source_version", "source_path",
            "target_schema", "target_version", "target_path", "status",
        })


class AuditSelectionTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema("Person", "1.0", {"type": "string"})
        self.c.register_schema("Address", "1.0", {"type": "string"})
        self.c.register_schema("Person", "2.0", {"type": "string"})

    def test_no_args_audits_all_in_registration_order(self):
        report = self.c.check_schema_references()
        self.assertEqual(report["checked"], 3)
        # checked/total 是源版本数；无引用源各自 resolved。
        self.assertEqual(report["resolved"], 3)

    def test_name_only_uses_registration_order(self):
        report = self.c.check_schema_references("Person")
        self.assertEqual(
            {(r["source_schema"], r["source_version"]) for r in report["references"]},
            set(),
        )
        self.assertEqual(report["checked"], 2)

    def test_version_only_matches_same_version(self):
        c = MetaCatalog()
        c.register_schema("Person", "1.0", obj(
            a={"$ref": "X@1.0#"}, b={"$ref": "Y@2.0#"}
        ))
        c.register_schema("Other", "2.0", obj(a={"$ref": "X@1.0#"}))
        report = c.check_schema_references(version="1.0")
        # 只有 Person@1.0 版本号为 1.0；其两条引用都在范围内。
        self.assertEqual(report["checked"], 1)
        self.assertEqual(
            {r["source_schema"] for r in report["references"]}, {"Person"}
        )
        self.assertEqual(
            {r["target_version"] for r in report["references"]}, {"1.0", "2.0"}
        )

    def test_both_args_selects_exact_version(self):
        report = self.c.check_schema_references("Person", "2.0")
        self.assertEqual(report["checked"], 1)

    def test_missing_versions_raise_not_found(self):
        with self.assertRaises(NotFoundError):
            self.c.check_schema_references("Person", "9.9")
        with self.assertRaises(NotFoundError):
            self.c.check_schema_references("Ghost")
        with self.assertRaises(NotFoundError):
            self.c.check_schema_references(version="9.9")

    def test_empty_catalog(self):
        report = MetaCatalog().check_schema_references()
        self.assertEqual(
            report,
            {"checked": 0, "total": 0, "resolved": 0, "issues": [], "references": []},
        )


class AuditOrderingAndStabilityTests(unittest.TestCase):
    def _catalog(self):
        c = MetaCatalog()
        c.register_schema("S", "1.0", obj(
            z={"$ref": "GoneZ@1.0#/z"},
            a={"$ref": "GoneA@1.0#/a"},
            m={"$ref": "AddrX@1.0#/properties/missing"},
        ))
        c.register_schema("AddrX", "1.0", obj(present={"type": "string"}))
        return c

    def test_issues_sorted_by_location_and_reason_equals_status(self):
        c = self._catalog()
        report = c.check_schema_references("S", "1.0")
        statuses = {
            (i["source_path"], i["target_schema"], i["target_path"]): i["reason"]
            for i in report["issues"]
        }
        for i in report["issues"]:
            self.assertEqual(
                i["reason"],
                statuses[(i["source_path"], i["target_schema"], i["target_path"])],
            )
            # reason 必须等于对应 reference 的 status。
            ref = next(
                r for r in report["references"]
                if (r["source_path"], r["target_schema"], r["target_path"])
                == (i["source_path"], i["target_schema"], i["target_path"])
            )
            self.assertEqual(i["reason"], ref["status"])
            self.assertTrue(i["message"])

        keys = [(i["source_path"], i["target_schema"], i["target_path"])
                for i in report["issues"]]
        segs = lambda p: tuple(p.strip("/").split("/")) if p else ()
        self.assertEqual(
            keys,
            sorted(keys, key=lambda k: (segs(k[0]), k[0], k[1], segs(k[2]), k[2])),
        )

    def test_same_input_stable_result(self):
        c = self._catalog()
        r1 = c.check_schema_references()
        r2 = c.check_schema_references()
        self.assertEqual(r1, r2)

    def test_result_is_json_serializable(self):
        c = self._catalog()
        json.dumps(c.check_schema_references(), ensure_ascii=False)

    def test_deduplicated_by_logical_six_tuple(self):
        c = MetaCatalog()
        c.register_schema("Tags", "1.0", {
            "type": "object",
            "properties": {
                "t": {"type": "array",
                      "items": {"$ref": "Word@1.0#/properties/v"}},
            },
        })
        c.register_schema("Word", "1.0", obj(v={"type": "string"}))
        report = c.check_schema_references("Tags", "1.0")
        rows = [(r["source_schema"], r["source_version"], r["source_path"],
                 r["target_schema"], r["target_version"], r["target_path"])
                for r in report["references"]]
        self.assertEqual(len(rows), len(set(rows)))
        self.assertEqual(rows[0][2], "/t/-")
        self.assertEqual(rows[0][5], "/v")


class AuditReadonlyTests(unittest.TestCase):
    def test_audit_does_not_mutate_or_index(self):
        c = MetaCatalog()
        c.register_schema("Person", "1.0", obj(
            ghost={"$ref": "Gone@1.0#/properties/g"},
            addr={"$ref": "Address@1.0#/properties/street"},
        ))
        c.register_schema("Address", "1.0", obj(street={"type": "string"}))

        before = c.get_schema("Person", "1.0")
        report = c.check_schema_references()
        after = c.get_schema("Person", "1.0")
        self.assertEqual(before, after)
        self.assertEqual(c.list_versions("Person"), ["1.0"])

        # 不入检索索引：缺失目标名无法被搜到，且没有生成变更报告。
        self.assertFalse(any(h["type"] == "schema" for h in c.search("Gone")))
        self.assertEqual(c.list_reports(), [])

        # 其它读取/分析结果不受影响。
        imp = c.analyze_impact("Address", "1.0", "/street")
        self.assertEqual(imp["direct_assets"], [])

        # 返回深拷贝：篡改结果不污染后续审计。
        report["issues"].append({"x": 1})
        report["references"][0]["status"] = "hacked"
        again = c.check_schema_references()
        self.assertNotIn("hacked", {r["status"] for r in again["references"]})
        self.assertEqual(len(again["issues"]), 1)

    def test_repeated_registration_still_already_exists(self):
        from meta_catalog.errors import AlreadyExistsError

        c = MetaCatalog()
        c.register_schema("Person", "1.0", {"type": "string"})
        with self.assertRaises(AlreadyExistsError):
            c.register_schema("Person", "1.0", {"type": "integer"})


if __name__ == "__main__":
    unittest.main()
