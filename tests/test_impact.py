import unittest

from meta_catalog import MetaCatalog
from meta_catalog.errors import NotFoundError


def schema(**props):
    return {"type": "object", "properties": props}


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.c = MetaCatalog()
        self.c.register_schema(
            "Address",
            "1.0",
            schema(street={"type": "string"}, zip={"type": "string"}),
        )
        self.c.register_schema(
            "Person",
            "1.0",
            schema(
                name={"type": "string"},
                address={"$ref": "Address@1.0#"},
            ),
        )
        self.c.register_schema(
            "Company",
            "1.0",
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

    def test_direct_assets(self):
        imp = self.c.analyze_impact("Address", "1.0", "/street")
        self.assertEqual([a["asset_id"] for a in imp["direct_assets"]], ["svc-mail"])

    def test_transitive_assets_multihop(self):
        imp = self.c.analyze_impact("Address", "1.0", "/street")
        trans = {(a["asset_id"], a["schema"], a["path"]) for a in imp["transitive_assets"]}
        self.assertIn(("svc-billing", "Person", "/address/street"), trans)
        self.assertIn(("svc-company", "Company", "/contact/street"), trans)

    def test_transitive_deduplicated_by_stable_path(self):
        # 同一资产经多条引用边到达同一稳定路径只出现一次。
        self.c.register_asset(
            "svc-dup", "重复服务", "service",
            [
                {"schema": "Person", "version": "1.0", "path": "/address/street"},
            ],
        )
        imp = self.c.analyze_impact("Address", "1.0", "/street")
        rows = [
            a for a in imp["transitive_assets"] if a["asset_id"] == "svc-dup"
        ]
        self.assertEqual(len(rows), 1)

    def test_transitive_order_is_stable(self):
        imp1 = self.c.analyze_impact("Address", "1.0", "/street")
        imp2 = self.c.analyze_impact("Address", "1.0", "/street")
        key = lambda rows: [(a["asset_id"], a["schema"], a["path"]) for a in rows]
        self.assertEqual(key(imp1["transitive_assets"]), key(imp2["transitive_assets"]))

    def test_root_change_propagates(self):
        # 整个 Address 根对象变更，引用 /address 或 /address/street 都受影响。
        self.c.register_asset(
            "svc-whole", "整对象服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/address"}],
        )
        imp = self.c.analyze_impact("Address", "1.0", "")
        ids = {a["asset_id"] for a in imp["transitive_assets"]}
        self.assertIn("svc-whole", ids)
        self.assertIn("svc-billing", ids)

    def test_unknown_version_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.c.analyze_impact("Address", "9.9", "/street")

    def test_cross_schema_cycle_terminates(self):
        c = MetaCatalog()
        c.register_schema("A", "1.0", schema(
            x={"type": "string"},
            b={"$ref": "B@1.0#"},
        ))
        c.register_schema("B", "1.0", schema(
            y={"type": "string"},
            a={"$ref": "A@1.0#"},
        ))
        c.register_asset(
            "svc-a", "A服务", "service",
            [{"schema": "A", "version": "1.0", "path": "/x"}],
        )
        # 双向环 + 资产引用，必须终止且不把起点错记为传递资产。
        imp = c.analyze_impact("A", "1.0", "/x")
        ids = [a["asset_id"] for a in imp["transitive_assets"]]
        self.assertEqual(ids.count("svc-a"), 0)


if __name__ == "__main__":
    unittest.main()
