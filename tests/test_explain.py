import unittest

from meta_catalog import MetaCatalog
from meta_catalog.errors import NotFoundError


def schema(**props):
    return {"type": "object", "properties": props}


def loc(schema_name, version, path):
    return {"schema": schema_name, "version": version, "path": path}


class ExplainImpactTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema(
            "Address", "1.0",
            schema(street={"type": "string"}, zip={"type": "string"}),
        )
        self.c.register_schema(
            "Person", "1.0",
            schema(name={"type": "string"}, address={"$ref": "Address@1.0#"}),
        )
        self.c.register_schema(
            "Company", "1.0",
            schema(contact={"$ref": "Person@1.0#/properties/address"}),
        )
        self.c.register_asset(
            "svc-mail", "邮寄服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/street"}],
        )
        self.c.register_asset(
            "svc-billing", "账单服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/address/street"}],
        )
        self.c.register_asset(
            "svc-company", "公司服务", "service",
            [{"schema": "Company", "version": "1.0", "path": "/contact/street"}],
        )

    def assets_by_id(self, result):
        return {a["asset_id"]: a for a in result["assets"]}

    def test_shape_and_direct_zero_step_chain(self):
        res = self.c.explain_impact("Address", "1.0", "/street")
        self.assertEqual(res["schema"], "Address")
        self.assertEqual(res["version"], "1.0")
        self.assertEqual(res["path"], "/street")
        mail = self.assets_by_id(res)["svc-mail"]
        self.assertEqual(mail["impact_kind"], "direct")
        self.assertEqual(
            mail["chains"],
            [{
                "source": loc("Address", "1.0", "/street"),
                "steps": [],
                "target": loc("Address", "1.0", "/street"),
            }],
        )

    def test_transitive_chain_single_hop(self):
        res = self.c.explain_impact("Address", "1.0", "/street")
        billing = self.assets_by_id(res)["svc-billing"]
        self.assertEqual(billing["impact_kind"], "transitive")
        self.assertEqual(
            billing["chains"],
            [{
                "source": loc("Person", "1.0", "/address/street"),
                "steps": [{
                    "from": loc("Person", "1.0", "/address/street"),
                    "to": loc("Address", "1.0", "/street"),
                }],
                "target": loc("Address", "1.0", "/street"),
            }],
        )

    def test_transitive_chain_multi_hop(self):
        res = self.c.explain_impact("Address", "1.0", "/street")
        company = self.assets_by_id(res)["svc-company"]
        self.assertEqual(company["impact_kind"], "transitive")
        self.assertEqual(len(company["chains"]), 1)
        chain = company["chains"][0]
        self.assertEqual(chain["source"], loc("Company", "1.0", "/contact/street"))
        self.assertEqual(chain["target"], loc("Address", "1.0", "/street"))
        self.assertEqual(
            chain["steps"],
            [
                {
                    "from": loc("Company", "1.0", "/contact/street"),
                    "to": loc("Person", "1.0", "/address/street"),
                },
                {
                    "from": loc("Person", "1.0", "/address/street"),
                    "to": loc("Address", "1.0", "/street"),
                },
            ],
        )

    def test_assets_sorted_by_asset_id(self):
        res = self.c.explain_impact("Address", "1.0", "/street")
        ids = [a["asset_id"] for a in res["assets"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, ["svc-billing", "svc-company", "svc-mail"])

    def test_impact_kind_both(self):
        self.c.register_asset(
            "svc-both", "双重服务", "service",
            [
                {"schema": "Address", "version": "1.0", "path": "/street"},
                {"schema": "Person", "version": "1.0", "path": "/address/street"},
            ],
        )
        res = self.c.explain_impact("Address", "1.0", "/street")
        both = self.assets_by_id(res)["svc-both"]
        self.assertEqual(both["impact_kind"], "both")
        kinds = sorted(len(c["steps"]) for c in both["chains"])
        self.assertEqual(kinds, [0, 1])  # 零步直接链 + 一步传递链分别保留

    def test_asset_ids_selection(self):
        res = self.c.explain_impact(
            "Address", "1.0", "/street", asset_ids=["svc-mail", "svc-billing", "svc-mail"]
        )
        self.assertEqual(
            [a["asset_id"] for a in res["assets"]], ["svc-billing", "svc-mail"]
        )
        # 顺序无关
        res2 = self.c.explain_impact(
            "Address", "1.0", "/street", asset_ids=["svc-billing", "svc-mail"]
        )
        self.assertEqual(res, res2)

    def test_asset_ids_empty_selects_nothing(self):
        res = self.c.explain_impact("Address", "1.0", "/street", asset_ids=[])
        self.assertEqual(res["assets"], [])

    def test_unknown_asset_id_raises(self):
        with self.assertRaises(NotFoundError) as ctx:
            self.c.explain_impact("Address", "1.0", "/street", asset_ids=["svc-none"])
        self.assertEqual(ctx.exception.details.get("asset_id"), "svc-none")

    def test_unknown_schema_or_version_raises(self):
        with self.assertRaises(NotFoundError) as ctx:
            self.c.explain_impact("Nope", "1.0", "/street")
        self.assertEqual(ctx.exception.details.get("schema"), "Nope")
        with self.assertRaises(NotFoundError) as ctx:
            self.c.explain_impact("Address", "9.9", "/street")
        self.assertEqual(ctx.exception.details.get("version"), "9.9")

    def test_unreachable_field_raises(self):
        with self.assertRaises(NotFoundError) as ctx:
            self.c.explain_impact("Address", "1.0", "/nope")
        self.assertEqual(ctx.exception.details.get("path"), "/nope")
        self.assertEqual(ctx.exception.details.get("schema"), "Address")

    def test_unaffected_asset_not_listed(self):
        self.c.register_asset(
            "svc-zip", "邮编服务", "service",
            [{"schema": "Address", "version": "1.0", "path": "/zip"}],
        )
        res = self.c.explain_impact("Address", "1.0", "/street")
        self.assertNotIn("svc-zip", self.assets_by_id(res))

    def test_ancestor_and_descendant_hits(self):
        self.c.register_asset(
            "svc-whole", "整对象服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/address"}],
        )
        # 查询根：后代路径命中。
        res = self.c.explain_impact("Address", "1.0", "")
        ids = self.assets_by_id(res)
        self.assertIn("svc-whole", ids)
        self.assertIn("svc-billing", ids)
        # 查询 /street：资产引用到达祖先路径 "" 也算命中。
        res2 = self.c.explain_impact("Address", "1.0", "/street")
        whole = self.assets_by_id(res2)["svc-whole"]
        self.assertEqual(whole["chains"][0]["target"], loc("Address", "1.0", ""))

    def test_shortest_chain_kept(self):
        c = MetaCatalog()
        c.register_schema("G", "1.0", schema(h={"type": "string"}))
        c.register_schema("E", "1.0", schema(b={"$ref": "G@1.0#"}))
        # D 根级引用 E，同时 /b 直接引用 G：同一 (source, target) 有两条链。
        c.register_schema("D", "1.0", {
            "$ref": "E@1.0#",
            "type": "object",
            "properties": {"b": {"$ref": "G@1.0#"}},
        })
        c.register_asset(
            "svc-d", "D服务", "service",
            [{"schema": "D", "version": "1.0", "path": "/b/h"}],
        )
        res = c.explain_impact("G", "1.0", "/h")
        chains = self.assets_by_id(res)["svc-d"]["chains"]
        self.assertEqual(len(chains), 1)
        self.assertEqual(
            chains[0]["steps"],
            [{"from": loc("D", "1.0", "/b/h"), "to": loc("G", "1.0", "/h")}],
        )

    def test_cycle_terminates_without_duplicate_chains(self):
        c = MetaCatalog()
        c.register_schema("A", "1.0", schema(
            x={"type": "string"}, b={"$ref": "B@1.0#"},
        ))
        c.register_schema("B", "1.0", schema(
            y={"type": "string"}, a={"$ref": "A@1.0#"},
        ))
        c.register_asset(
            "svc-a", "A服务", "service",
            [{"schema": "A", "version": "1.0", "path": "/x"}],
        )
        res = c.explain_impact("A", "1.0", "/x")
        entry = self.assets_by_id(res)["svc-a"]
        self.assertEqual(entry["impact_kind"], "direct")
        self.assertEqual(len(entry["chains"]), 1)

    def test_deterministic_and_independent_copy(self):
        r1 = self.c.explain_impact("Address", "1.0", "/street")
        r2 = self.c.explain_impact("Address", "1.0", "/street")
        self.assertEqual(r1, r2)
        r1["assets"][0]["chains"].append({"source": {}, "steps": [], "target": {}})
        r1["assets"].clear()
        r3 = self.c.explain_impact("Address", "1.0", "/street")
        self.assertEqual(r2, r3)

    def test_read_only_side_effects(self):
        before_reports = self.c.list_reports()
        before_search = self.c.search("street")
        self.c.explain_impact("Address", "1.0", "/street")
        self.assertEqual(self.c.list_reports(), before_reports)
        self.assertEqual(self.c.search("street"), before_search)


if __name__ == "__main__":
    unittest.main()
