import json
import unittest

from meta_catalog import MetaCatalog, limits
from meta_catalog.errors import ImpactAnalysisTooLarge, NotFoundError


def schema(**props):
    return {"type": "object", "properties": props}


class ExplainImpactTests(unittest.TestCase):
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

    # ------------------------------------------------------------- 结构形状
    def test_top_level_shape_and_field_order(self):
        out = self.c.explain_impact("Address", "1.0", "/street")
        self.assertEqual(list(out.keys()), ["schema", "version", "path", "assets"])
        self.assertEqual(out["schema"], "Address")
        self.assertEqual(out["version"], "1.0")
        self.assertEqual(out["path"], "/street")
        json.dumps(out, ensure_ascii=False)  # 可 JSON 序列化
        for asset in out["assets"]:
            self.assertEqual(
                list(asset.keys()),
                ["asset_id", "name", "kind", "impact_kind", "chains"],
            )
            for chain in asset["chains"]:
                self.assertEqual(list(chain.keys()), ["source", "steps", "target"])
                self.assertEqual(
                    list(chain["source"].keys()), ["schema", "version", "path"]
                )
                self.assertEqual(
                    list(chain["target"].keys()), ["schema", "version", "path"]
                )
                for step in chain["steps"]:
                    self.assertEqual(list(step.keys()), ["from", "to"])
                    self.assertEqual(
                        list(step["from"].keys()), ["schema", "version", "path"]
                    )
                    self.assertEqual(
                        list(step["to"].keys()), ["schema", "version", "path"]
                    )

    def test_assets_sorted_and_only_impacted_listed(self):
        out = self.c.explain_impact("Address", "1.0", "/street")
        ids = [a["asset_id"] for a in out["assets"]]
        self.assertEqual(ids, ["svc-billing", "svc-company", "svc-mail"])

    # ------------------------------------------------------------- 直接命中
    def test_direct_chain_is_zero_step(self):
        out = self.c.explain_impact("Address", "1.0", "/street")
        mail = next(a for a in out["assets"] if a["asset_id"] == "svc-mail")
        self.assertEqual(mail["impact_kind"], "direct")
        self.assertEqual(mail["name"], "邮寄服务")
        self.assertEqual(mail["kind"], "service")
        (chain,) = mail["chains"]
        loc = {"schema": "Address", "version": "1.0", "path": "/street"}
        self.assertEqual(chain["source"], loc)
        self.assertEqual(chain["target"], loc)
        self.assertEqual(chain["steps"], [])

    # ------------------------------------------------------------- 传递命中
    def test_transitive_chain_single_hop(self):
        out = self.c.explain_impact("Address", "1.0", "/street")
        billing = next(a for a in out["assets"] if a["asset_id"] == "svc-billing")
        self.assertEqual(billing["impact_kind"], "transitive")
        (chain,) = billing["chains"]
        self.assertEqual(
            chain["source"],
            {"schema": "Person", "version": "1.0", "path": "/address/street"},
        )
        self.assertEqual(
            chain["target"],
            {"schema": "Address", "version": "1.0", "path": "/street"},
        )
        self.assertEqual(
            chain["steps"],
            [
                {
                    "from": {"schema": "Person", "version": "1.0",
                             "path": "/address/street"},
                    "to": {"schema": "Address", "version": "1.0", "path": "/street"},
                }
            ],
        )

    def test_transitive_chain_multi_hop(self):
        out = self.c.explain_impact("Address", "1.0", "/street")
        company = next(a for a in out["assets"] if a["asset_id"] == "svc-company")
        self.assertEqual(company["impact_kind"], "transitive")
        (chain,) = company["chains"]
        self.assertEqual(
            chain["source"],
            {"schema": "Company", "version": "1.0", "path": "/contact/street"},
        )
        self.assertEqual(
            chain["target"],
            {"schema": "Address", "version": "1.0", "path": "/street"},
        )
        self.assertEqual(
            [(s["from"]["schema"], s["to"]["schema"]) for s in chain["steps"]],
            [("Company", "Person"), ("Person", "Address")],
        )
        # 链路连续：首步 from 为 source，末步 to 为 target，中间首尾相接。
        self.assertEqual(chain["steps"][0]["from"], chain["source"])
        self.assertEqual(chain["steps"][-1]["to"], chain["target"])
        for prev, nxt in zip(chain["steps"], chain["steps"][1:]):
            self.assertEqual(prev["to"], nxt["from"])

    def test_impact_kind_both_keeps_direct_and_transitive_chains(self):
        self.c.register_asset(
            "svc-both", "双重服务", "service",
            [
                {"schema": "Address", "version": "1.0", "path": "/street"},
                {"schema": "Person", "version": "1.0", "path": "/address/street"},
            ],
        )
        out = self.c.explain_impact("Address", "1.0", "/street")
        both = next(a for a in out["assets"] if a["asset_id"] == "svc-both")
        self.assertEqual(both["impact_kind"], "both")
        self.assertEqual(len(both["chains"]), 2)
        zero = [ch for ch in both["chains"] if ch["steps"] == []]
        walked = [ch for ch in both["chains"] if ch["steps"]]
        self.assertEqual(len(zero), 1)
        self.assertEqual(len(walked), 1)
        self.assertEqual(zero[0]["source"], zero[0]["target"])
        self.assertEqual(walked[0]["source"]["schema"], "Person")

    # ------------------------------------------------------- 祖先/后代包含
    def test_ancestor_and_descendant_containment_hit(self):
        self.c.register_asset(
            "svc-whole", "整对象服务", "service",
            [{"schema": "Person", "version": "1.0", "path": "/address"}],
        )
        # 所查字段是命中路径的后代：/address 经引用覆盖 Address 根。
        out = self.c.explain_impact("Address", "1.0", "")
        ids = {a["asset_id"] for a in out["assets"]}
        self.assertIn("svc-whole", ids)
        self.assertIn("svc-billing", ids)
        whole = next(a for a in out["assets"] if a["asset_id"] == "svc-whole")
        (chain,) = whole["chains"]
        self.assertEqual(chain["target"]["path"], "")
        # 所查字段是命中路径的祖先：/address/street 命中 Address /street。
        out2 = self.c.explain_impact("Address", "1.0", "/street")
        ids2 = {a["asset_id"] for a in out2["assets"]}
        self.assertIn("svc-whole", ids2)

    # -------------------------------------------------------------- asset_ids
    def test_asset_ids_dedup_and_order_independent(self):
        a = self.c.explain_impact(
            "Address", "1.0", "/street",
            asset_ids=["svc-mail", "svc-billing", "svc-mail"],
        )
        b = self.c.explain_impact(
            "Address", "1.0", "/street",
            asset_ids=["svc-billing", "svc-mail"],
        )
        self.assertEqual(a, b)
        self.assertEqual(
            [x["asset_id"] for x in a["assets"]], ["svc-billing", "svc-mail"]
        )

    def test_empty_asset_ids_selects_nothing(self):
        out = self.c.explain_impact("Address", "1.0", "/street", asset_ids=[])
        self.assertEqual(out["assets"], [])

    def test_asset_without_hit_not_listed(self):
        out = self.c.explain_impact(
            "Address", "1.0", "/street", asset_ids=["svc-mail", "svc-billing"]
        )
        self.assertEqual(
            [a["asset_id"] for a in out["assets"]], ["svc-billing", "svc-mail"]
        )
        out = self.c.explain_impact("Address", "1.0", "/zip")
        self.assertEqual(out["assets"], [])

    # ------------------------------------------------------------------ 错误
    def test_unknown_schema_or_version(self):
        with self.assertRaises(NotFoundError) as cm:
            self.c.explain_impact("Missing", "1.0", "/street")
        self.assertEqual(cm.exception.details["schema"], "Missing")
        with self.assertRaises(NotFoundError) as cm:
            self.c.explain_impact("Address", "9.9", "/street")
        self.assertEqual(cm.exception.details["schema"], "Address")
        self.assertEqual(cm.exception.details["version"], "9.9")

    def test_unreachable_field(self):
        with self.assertRaises(NotFoundError) as cm:
            self.c.explain_impact("Address", "1.0", "/nope")
        self.assertEqual(cm.exception.details["schema"], "Address")
        self.assertEqual(cm.exception.details["version"], "1.0")
        self.assertEqual(cm.exception.details["path"], "/nope")

    def test_unknown_asset_id(self):
        with self.assertRaises(NotFoundError) as cm:
            self.c.explain_impact(
                "Address", "1.0", "/street", asset_ids=["svc-missing"]
            )
        self.assertEqual(cm.exception.details["asset_id"], "svc-missing")

    # ------------------------------------------------------- 最短链与引用环
    def test_shortest_chain_kept_with_stable_tie_break(self):
        c = MetaCatalog()
        c.register_schema("W", "1.0", schema(f={"type": "string"}))
        c.register_schema("Y", "1.0", schema(a={"$ref": "W@1.0#"}))
        c.register_schema("Z", "1.0", {"$ref": "W@1.0#"})
        # S 根引用 Y、/a 引用 Z：从 S/a 出发存在两条等长（2 步）链到达 W 根：
        # S/a -> Y/a -> W 与 S/a -> Z -> W。
        c.register_schema(
            "S", "1.0",
            {
                "type": "object",
                "$ref": "Y@1.0#",
                "properties": {"a": {"$ref": "Z@1.0#"}},
            },
        )
        c.register_asset(
            "svc-s", "S服务", "service",
            [{"schema": "S", "version": "1.0", "path": "/a"}],
        )
        out = c.explain_impact("W", "1.0", "")
        (asset,) = out["assets"]
        # 同一 (source, target) 只保留一条最短链；等长时按步进定位元组
        # 取稳定最小者（经 Y 而非 Z）。
        (chain,) = asset["chains"]
        self.assertEqual(
            chain["source"], {"schema": "S", "version": "1.0", "path": "/a"}
        )
        self.assertEqual(
            chain["target"], {"schema": "W", "version": "1.0", "path": ""}
        )
        self.assertEqual(len(chain["steps"]), 2)
        self.assertEqual(chain["steps"][0]["to"]["schema"], "Y")

    def test_reference_cycle_terminates_without_duplicate_chains(self):
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
            "svc-b", "B服务", "service",
            [{"schema": "B", "version": "1.0", "path": "/a/x"}],
        )
        out = c.explain_impact("A", "1.0", "/x")
        (asset,) = out["assets"]
        self.assertEqual(asset["asset_id"], "svc-b")
        (chain,) = asset["chains"]
        self.assertEqual(chain["source"]["path"], "/a/x")
        self.assertEqual(
            chain["target"], {"schema": "A", "version": "1.0", "path": "/x"}
        )
        self.assertEqual(len(chain["steps"]), 1)
        # 重复调用结果一致，环不产生重复链或无界结果。
        out2 = c.explain_impact("A", "1.0", "/x")
        self.assertEqual(out, out2)

    # ------------------------------------------------------------- 只读语义
    def test_read_only_and_deterministic(self):
        versions_before = self.c.list_versions("Address")
        search_before = self.c.search("street")
        reports_before = self.c.list_reports()

        r1 = self.c.explain_impact("Address", "1.0", "/street")
        r1["assets"][0]["chains"].clear()
        r1["path"] = "tampered"
        r2 = self.c.explain_impact("Address", "1.0", "/street")

        self.assertEqual(r2["path"], "/street")
        self.assertTrue(all(a["chains"] for a in r2["assets"]))
        r3 = self.c.explain_impact("Address", "1.0", "/street")
        self.assertEqual(r2, r3)

        self.assertEqual(self.c.list_versions("Address"), versions_before)
        self.assertEqual(self.c.search("street"), search_before)
        self.assertEqual(self.c.list_reports(), reports_before)

    def test_existing_entries_unchanged(self):
        # 既有公开入口的返回不受影响。
        imp = self.c.explain_impact("Address", "1.0", "/street")
        ana = self.c.analyze_impact("Address", "1.0", "/street")
        self.assertEqual(
            {a["asset_id"] for a in imp["assets"]},
            {a["asset_id"] for a in ana["direct_assets"]}
            | {a["asset_id"] for a in ana["transitive_assets"]},
        )

    # ------------------------------------------------------------------ 限制
    def test_depth_limit_raises_too_large(self):
        c = MetaCatalog()
        c.register_schema("L1", "1.0", schema(n={"$ref": "L2@1.0#"}))
        c.register_schema("L2", "1.0", schema(n={"$ref": "L3@1.0#"}))
        c.register_schema("L3", "1.0", schema(f={"type": "string"}))
        c.register_asset(
            "svc-l", "链式服务", "service",
            [{"schema": "L1", "version": "1.0", "path": "/n/n/f"}],
        )
        old = limits.MAX_IMPACT_DEPTH
        limits.MAX_IMPACT_DEPTH = 1
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                c.explain_impact("L3", "1.0", "/f")
            self.assertEqual(cm.exception.details["reason"], "depth_exceeded")
        finally:
            limits.MAX_IMPACT_DEPTH = old

    def test_assets_limit_raises_too_large(self):
        old = limits.MAX_IMPACT_ASSETS
        limits.MAX_IMPACT_ASSETS = 1
        try:
            with self.assertRaises(ImpactAnalysisTooLarge) as cm:
                self.c.explain_impact("Address", "1.0", "/street")
            self.assertEqual(cm.exception.details["reason"], "assets_exceeded")
        finally:
            limits.MAX_IMPACT_ASSETS = old


if __name__ == "__main__":
    unittest.main()
