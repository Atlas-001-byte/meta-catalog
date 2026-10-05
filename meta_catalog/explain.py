"""字段影响链路解释。

在字段级影响分析（:mod:`meta_catalog.impact`）的命中口径之上，解释每个资产
*为什么* 被命中：把直接引用与沿跨 Schema ``$ref`` 的传递解析展开为显式链路。

链路约定：

  * 直接命中是零步链，``source`` 与 ``target`` 都等于所查字段；
  * 传递命中的 ``source`` 是资产引用的具体字段，``target`` 是所查 Schema
    版本中的命中路径（与所查路径相等、或存在祖先/后代包含关系），``steps``
    记录每一次跨 Schema 跳转的 ``from``/``to`` 定位；
  * 同一 ``(source, target)`` 只保留一条最短链，等长时按步进定位元组
    （逐步 ``(from, to)`` 定位）取稳定最小者；同一资产的直接与传递来源
    分别保留，``impact_kind`` 按实际命中合并为 ``direct`` / ``transitive``
    / ``both``。

本模块只读注册簿：不注册资源、不生成报告、不写入检索索引。
"""

from __future__ import annotations

import heapq
from typing import Any

from . import impact as impact_mod
from . import limits, pointer as ptr
from .errors import ImpactAnalysisTooLarge, NotFoundError
from .registry import Registry

# 步进定位元组：(from_schema, from_version, from_path, to_schema, to_version, to_path)
_StepKey = tuple[str, str, str, str, str, str]


def _locator(schema: str, version: str, path: str) -> dict[str, str]:
    return {"schema": schema, "version": version, "path": path}


def _resolve_asset_ids(registry: Registry, asset_ids: list[str] | None) -> list[str]:
    """校验并去重资产选择，返回按 asset_id 升序的入选清单。

    ``None`` 表示覆盖全部资产；空列表不选任何资产；未知资产抛
    :class:`NotFoundError`。
    """
    if asset_ids is None:
        return [a.id for a in registry.all_assets()]
    unique: set[str] = set()
    for aid in asset_ids:
        if not isinstance(aid, str) or not aid:
            raise NotFoundError(f"资产 {aid!r} 不存在", details={"asset_id": aid})
        if aid in unique:
            continue
        try:
            registry.get_asset(aid)
        except NotFoundError as exc:
            raise NotFoundError(
                f"资产 {aid} 不存在", details={"asset_id": aid}
            ) from exc
        unique.add(aid)
    return sorted(unique)


def _forward_chains(
    registry: Registry,
    schema: str,
    version: str,
    path: str,
    outgoing: dict[tuple[str, str], list],
    cache: dict[tuple[str, str, str], dict],
) -> dict:
    """从具体字段正向解析跨 Schema 引用，返回 ``{到达字段: 最短步进序列}``。

    可达口径与 :func:`meta_catalog.impact.forward_reach` 一致（结果不含起点
    自身），但为每个到达字段保留一条最短链；等长时按步进定位元组取稳定
    最小者。步进序列中每项为 ``(from Reach, to Reach)``。
    """
    key = (schema, version, path)
    cached = cache.get(key)
    if cached is not None:
        return cached

    start = impact_mod.Reach(schema, version, path)
    # state -> (深度, 步进定位元组序列, 步进序列)；堆按 (深度, 定位元组) 弹出，
    # 首次弹出即该状态的最优链（深度优先先、同深度定位元组小者优先）。
    best: dict[Any, tuple[int, tuple[_StepKey, ...], tuple]] = {start: (0, (), ())}
    heap: list[tuple[int, tuple[_StepKey, ...], Any]] = [(0, (), start)]
    edges_walked = 0
    chains: dict[Any, tuple] = {}

    while heap:
        depth, step_keys, state = heapq.heappop(heap)
        cur = best[state]
        if cur[0] != depth or cur[1] != step_keys:
            continue  # 过期堆项：已有更优链
        if state != start:
            chains[state] = cur[2]
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
            suffix = cp[len(sp):]
            mapped = impact_mod.Reach(
                edge.dst_schema,
                edge.dst_version,
                ptr.format(list(ptr.parse(edge.dst_path)) + list(suffix)),
            )
            step_key = (
                state.schema,
                state.version,
                state.path,
                mapped.schema,
                mapped.version,
                mapped.path,
            )
            candidate = (depth + 1, step_keys + (step_key,))
            known = best.get(mapped)
            if known is not None and (known[0], known[1]) <= candidate:
                continue
            best[mapped] = (candidate[0], candidate[1], cur[2] + ((state, mapped),))
            heapq.heappush(heap, (candidate[0], candidate[1], mapped))

    cache[key] = chains
    return chains


def explain_field(
    registry: Registry,
    schema: str,
    version: str,
    path: str,
    asset_ids: list[str] | None,
) -> dict[str, Any]:
    """解释指定字段对各资产的直接/传递影响链路（只读）。"""
    registry.get_schema(schema, version)  # 未知 Schema/版本 → NotFoundError
    if not registry._logical_path_exists(
        schema, version, path, frozenset({(schema, version)})
    ):
        raise NotFoundError(
            f"字段 {schema}@{version}{path} 不可达",
            details={"schema": schema, "version": version, "path": path},
        )
    selected = _resolve_asset_ids(registry, asset_ids)
    assets = {a.id: a for a in registry.all_assets()}
    outgoing = impact_mod._build_outgoing(registry)
    chain_cache: dict[tuple[str, str, str], dict] = {}

    direct_ids: set[str] = set()
    transitive_ids: set[str] = set()
    entries: list[dict[str, Any]] = []

    for aid in selected:
        asset = assets[aid]
        # (source 定位, target 定位) -> chain；同一对只保留一条最短链。
        chains_by_key: dict[tuple[tuple[str, str, str], tuple[str, str, str]], dict] = {}
        has_direct = False
        has_transitive = False

        for ref in asset.refs:
            if ref.schema == schema and ref.version == version and ref.path == path:
                # 直接命中：零步链，source 与 target 都等于所查字段。
                key = ((schema, version, path), (schema, version, path))
                chains_by_key[key] = {
                    "source": _locator(schema, version, path),
                    "steps": [],
                    "target": _locator(schema, version, path),
                }
                has_direct = True
                continue

            reached = _forward_chains(
                registry, ref.schema, ref.version, ref.path, outgoing, chain_cache
            )
            matched = sorted(
                (
                    r
                    for r in reached
                    if r.schema == schema
                    and r.version == version
                    and impact_mod._related(r.path, path)
                ),
                key=lambda r: (r.schema, r.version, r.path),
            )
            if not matched:
                continue
            has_transitive = True
            for r in matched:
                source = (ref.schema, ref.version, ref.path)
                target = (r.schema, r.version, r.path)
                chains_by_key[(source, target)] = {
                    "source": _locator(*source),
                    "steps": [
                        {
                            "from": _locator(f.schema, f.version, f.path),
                            "to": _locator(t.schema, t.version, t.path),
                        }
                        for f, t in reached[r]
                    ],
                    "target": _locator(*target),
                }

        if not chains_by_key:
            continue
        if has_direct:
            direct_ids.add(aid)
        if has_transitive:
            transitive_ids.add(aid)
        impact_kind = (
            "both" if has_direct and has_transitive
            else "direct" if has_direct
            else "transitive"
        )
        entries.append(
            {
                "asset_id": asset.id,
                "name": asset.name,
                "kind": asset.kind,
                "impact_kind": impact_kind,
                "chains": [chains_by_key[k] for k in sorted(chains_by_key)],
            }
        )

    if len(direct_ids) + len(transitive_ids) > limits.MAX_IMPACT_ASSETS:
        raise ImpactAnalysisTooLarge(
            "影响资产数量超过公开限制",
            details={"reason": "assets_exceeded", "limit": limits.MAX_IMPACT_ASSETS},
        )

    return {
        "schema": schema,
        "version": version,
        "path": path,
        "assets": entries,
    }
