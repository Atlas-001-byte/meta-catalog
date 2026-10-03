"""字段级影响分析。

直接影响：资产的字段引用与目标字段 ``(schema, version, path)`` 精确相等。
传递影响：从资产引用的具体字段出发，沿跨 Schema ``$ref`` 边正向解析，
若能到达目标字段（精确相等、或祖先/后代包含关系），即构成传递命中。

正向解析从资产引用的 *具体* 路径出发，路径长度随跨边跳数只减不增，因此
即使 Schema 之间存在根级引用环，状态空间也是有限的。传递结果按稳定标识
``(资产, schema, version, 引用路径)`` 去重。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import limits, pointer as ptr
from .errors import ImpactAnalysisTooLarge
from .registry import Registry, RefEdge


@dataclass(frozen=True)
class Reach:
    schema: str
    version: str
    path: str


def _related(a: str, b: str) -> bool:
    return a == b or ptr.is_under(a, b) or ptr.is_under(b, a)


def _build_outgoing(registry: Registry) -> dict[tuple[str, str], list[RefEdge]]:
    outgoing: dict[tuple[str, str], list[RefEdge]] = {}
    for edge in registry.all_edges():
        outgoing.setdefault((edge.src_schema, edge.src_version), []).append(edge)
    for edges in outgoing.values():
        edges.sort(
            key=lambda e: (e.dst_schema, e.dst_version, e.src_path, e.dst_path)
        )
    return outgoing


def forward_reach(
    registry: Registry,
    schema: str,
    version: str,
    path: str,
    outgoing: dict[tuple[str, str], list[RefEdge]] | None = None,
    cache: dict[tuple[str, str, str], list[Reach]] | None = None,
) -> list[Reach]:
    """从一个具体字段出发正向解析跨 Schema 引用，返回可到达的全部字段。

    每条出边满足：边的源路径与当前路径相等，或是当前路径的祖先
    （引用了包含当前字段的子树）；目标侧路径按后缀对齐。
    """
    if cache is not None:
        cached = cache.get((schema, version, path))
        if cached is not None:
            return cached
    if outgoing is None:
        outgoing = _build_outgoing(registry)

    start = Reach(schema, version, path)
    reached: dict[Reach, None] = {start: None}
    queue: list[tuple[Reach, int]] = [(start, 0)]
    edges_walked = 0

    while queue:
        state, depth = queue.pop(0)
        if depth >= limits.MAX_IMPACT_DEPTH:
            raise ImpactAnalysisTooLarge(
                "影响链深度超过公开限制",
                details={
                    "reason": "depth_exceeded",
                    "limit": limits.MAX_IMPACT_DEPTH,
                    "schema": state.schema,
                    "version": state.version,
                    "path": state.path,
                },
            )
        for edge in outgoing.get((state.schema, state.version), ()):
            sp, cp = ptr.parse(edge.src_path), ptr.parse(state.path)
            if not (len(sp) <= len(cp) and cp[: len(sp)] == sp):
                continue
            edges_walked += 1
            if edges_walked > limits.MAX_IMPACT_VISITED:
                raise ImpactAnalysisTooLarge(
                    "影响链引用边数量超过公开限制",
                    details={"reason": "edges_exceeded", "limit": limits.MAX_IMPACT_VISITED},
                )
            suffix = cp[len(sp) :]
            mapped = Reach(
                edge.dst_schema,
                edge.dst_version,
                ptr.format(list(ptr.parse(edge.dst_path)) + list(suffix)),
            )
            if mapped in reached:
                continue
            reached[mapped] = None
            queue.append((mapped, depth + 1))

    del reached[start]
    result = sorted(reached.keys(), key=lambda r: (r.schema, r.version, r.path))
    if cache is not None:
        cache[(schema, version, path)] = result
    return result


def analyze_field(registry: Registry, schema: str, version: str, path: str) -> dict:
    """返回单个字段的影响资产：直接 + 传递（各自稳定排序、去重）。"""
    registry.get_schema(schema, version)

    direct: dict[str, dict] = {}
    transitive_map: dict[tuple[str, str, str, str], dict] = {}
    outgoing = _build_outgoing(registry)
    reach_cache: dict[tuple[str, str, str], list[Reach]] = {}

    for asset in registry.all_assets():
        for ref in asset.refs:
            if ref.schema == schema and ref.version == version:
                if ref.path == path:
                    direct.setdefault(
                        asset.id,
                        {"asset_id": asset.id, "name": asset.name, "kind": asset.kind},
                    )
                    continue

            # 传递：从资产引用的具体字段正向解析到目标字段。
            reachable = forward_reach(
                registry, ref.schema, ref.version, ref.path, outgoing, reach_cache
            )
            matched = [r for r in reachable if r.schema == schema and r.version == version
                       and _related(r.path, path)]
            if not matched:
                continue
            key = (asset.id, ref.schema, ref.version, ref.path)
            entry = transitive_map.get(key)
            if entry is None:
                entry = {
                    "asset_id": asset.id,
                    "name": asset.name,
                    "kind": asset.kind,
                    "schema": ref.schema,
                    "version": ref.version,
                    "path": ref.path,
                    "matched_paths": [],
                }
                transitive_map[key] = entry
            for r in matched:
                if r.path not in entry["matched_paths"]:
                    entry["matched_paths"].append(r.path)

    transitive = list(transitive_map.values())
    for entry in transitive:
        entry["matched_paths"].sort(key=lambda p: (ptr.parse(p), p))

    if len(direct) + len(transitive) > limits.MAX_IMPACT_ASSETS:
        raise ImpactAnalysisTooLarge(
            "影响资产数量超过公开限制",
            details={"reason": "assets_exceeded", "limit": limits.MAX_IMPACT_ASSETS},
        )

    return {
        "schema": schema,
        "version": version,
        "path": path,
        "direct_assets": [direct[k] for k in sorted(direct)],
        "transitive_assets": sorted(
            transitive,
            key=lambda a: (a["asset_id"], a["schema"], a["version"], a["path"]),
        ),
    }
