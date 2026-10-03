"""影响分析：直接引用与跨 Schema 传递引用。

从变更字段路径出发，沿 ``$ref`` 反向边逐层回溯引用方 Schema，
收集引用受影响字段子树的资产。传递链按稳定路径去重，每个资产
保留最短（并列时字典序最小）的一条链。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from .errors import ImpactAnalysisTooLarge
from .limits import LIMITS
from .registry import Asset, Registry, _paths_overlap

Node = tuple[str, str, str]  # (schema 名称, 版本, 受影响字段路径)
Chain = tuple[Node, ...]


@dataclass(frozen=True)
class ImpactedAsset:
    asset_id: str
    name: str
    asset_type: str
    # 1 = 直接引用变更字段；>1 = 经 depth-1 个 Schema 传递
    depth: int
    # 资产在末端 Schema 上命中的引用字段路径
    via_fields: tuple[str, ...]
    # 从变更 Schema 到末端 Schema 的稳定节点链
    chain: Chain

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "name": self.name,
            "type": self.asset_type,
            "depth": self.depth,
            "via_fields": [p or "/" for p in self.via_fields],
            "chain": [
                {"schema": n[0], "version": n[1], "field_path": n[2] or "/"}
                for n in self.chain
            ],
        }


@dataclass
class _AssetHit:
    depth: int
    chain: Chain
    via_fields: tuple[str, ...]


class ImpactAnalyzer:
    def __init__(self, registry: Registry):
        self._registry = registry

    def analyze(
        self,
        schema_name: str,
        version: str,
        changed_paths: list[str],
    ) -> dict[str, list[ImpactedAsset]]:
        """返回 ``变更字段路径 -> 受影响资产（有序、已去重）``。"""
        roots = sorted(set(changed_paths))
        if len(roots) > LIMITS["max_fields_per_comparison"]:
            raise ImpactAnalysisTooLarge("analysis object exceeds public field limit")
        # 每个节点记录：根变更路径 -> (深度, 链)
        reach: dict[Node, dict[str, tuple[int, Chain]]] = {}
        # 每个根路径记录：asset_id -> 最佳命中
        hits: dict[str, dict[str, _AssetHit]] = {root: {} for root in roots}

        queue: deque[tuple[Node, str, int, Chain]] = deque()
        for root in roots:
            node = (schema_name, version, root)
            chain = (node,)
            reach.setdefault(node, {})[root] = (0, chain)
            queue.append((node, root, 0, chain))

        while queue:
            (name, ver, path), root, depth, chain = queue.popleft()
            known = reach.get((name, ver, path), {}).get(root)
            # 仅处理当前最优条目；同深度更短链晚到时重新处理一次。
            if known is None or known != (depth, chain):
                continue

            # 该节点上直接/传递引用此子树的资产
            assets = self._registry.assets_referencing(name, ver, {path})
            for asset in assets:
                root_hits = hits[root]
                via = tuple(
                    sorted(
                        ref.field_path
                        for ref in asset.references
                        if ref.schema_name == name
                        and self._registry.resolve_version(name, ref.version) == ver
                        and _paths_overlap(ref.field_path, path)
                    )
                )
                candidate = _AssetHit(depth + 1, chain, via or (path,))
                best = root_hits.get(asset.asset_id)
                if best is None or (candidate.depth, candidate.chain) < (best.depth, best.chain):
                    if best is None and len(root_hits) >= LIMITS["max_impacted_assets"]:
                        raise ImpactAnalysisTooLarge(
                            "impacted asset count exceeds public limit"
                        )
                    root_hits[asset.asset_id] = candidate

            if depth >= LIMITS["max_impact_depth"]:
                # 仍有可继续扩展的边才算超长影响链
                if self._expandable((name, ver, path)):
                    raise ImpactAnalysisTooLarge(
                        "impact chain depth exceeds public limit"
                    )
                continue

            for child in self._reverse_neighbors((name, ver, path)):
                child_node = child[0]
                child_depth = depth + 1
                child_chain = chain + (child_node,)
                bucket = reach.setdefault(child_node, {})
                prev = bucket.get(root)
                if prev is not None and (prev[0], prev[1]) <= (child_depth, child_chain):
                    continue
                bucket[root] = (child_depth, child_chain)
                queue.append((child_node, root, child_depth, child_chain))

        result: dict[str, list[ImpactedAsset]] = {}
        for root in roots:
            entries: list[ImpactedAsset] = []
            for asset_id, hit in hits[root].items():
                asset = self._registry.get_asset(asset_id)
                entries.append(
                    ImpactedAsset(
                        asset_id=asset.asset_id,
                        name=asset.name,
                        asset_type=asset.asset_type,
                        depth=hit.depth,
                        via_fields=hit.via_fields,
                        chain=hit.chain,
                    )
                )
            entries.sort(key=lambda a: (a.depth, a.asset_id, a.name))
            result[root] = entries
        return result

    # ------------------------------------------------------------------

    def _expandable(self, node: Node) -> bool:
        return bool(self._reverse_neighbors(node))

    def _reverse_neighbors(self, node: Node) -> list[tuple[Node, str]]:
        name, version, path = node
        out: list[tuple[Node, str]] = []
        for edge in self._registry.reverse_edges(name):
            resolved = self._registry.resolve_version(name, edge.target_version)
            if resolved != version:
                continue
            if not _paths_overlap(edge.target_pointer, path):
                continue
            out.append((
                (edge.source_name, edge.source_version, edge.source_field_path),
                edge.target_pointer,
            ))
        out.sort(key=lambda item: item[0])
        return out
