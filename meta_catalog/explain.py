"""字段影响链路解释。

在字段级影响分析（:mod:`meta_catalog.impact`）的命中口径之上，解释每个资产
*为什么* 受所查字段影响：

  * 直接命中：资产引用的字段与所查字段精确相等，给出零步链，
    ``source == target == 所查字段``；
  * 传递命中：从资产引用的具体字段出发，沿跨 Schema ``$ref`` 边正向解析，
    到达所查版本中与所查字段相等或祖先/后代包含的命中路径，给出实际步进链。

同一 ``(source, target)`` 只保留一条最短链；等长时按步进定位元组
（逐步 ``from``/``to`` 的 ``(schema, version, path)``）取稳定最小者。
同一资产的直接与传递来源分别保留，``impact_kind`` 按实际命中合并为
``direct`` / ``transitive`` / ``both``。

正向解析路径长度随跨边跳数只减不增，引用环不会产生重复链或无界结果。
本模块只读注册簿：不注册资源、不生成报告、不写入检索索引。
"""

from __future__ import annotations

from typing import Any

from . import limits, pointer as ptr
from .errors import ImpactAnalysisTooLarge, NotFoundError
from .impact import _build_outgoing, _related
from .registry import Registry

# 状态定位：(schema, version, path) 三元组。
_State = tuple[str, str, str]


def _locator(state: _State) -> dict[str, Any]:
    return {"schema": state[0], "version": state[1], "path": state[2]}


def _resolve_asset_ids(
    registry: Registry, asset_ids: list[str] | None
) -> list[str]:
    """校验并去重资产选择，返回按 asset_id 升序的入选清单。

    ``None`` 覆盖全部资产；空列表不选任何资产；未知资产抛
    :class:`NotFoundError`。
    """
    if asset_ids is None:
        return [a.id for a in registry.all_assets()]
    known = {a.id for a in registry.all_assets()}
    unique: set[str] = set()
    for aid in asset_ids:
        if not isinstance(aid, str) or not aid or aid not in known:
            raise NotFoundError(f"资产 {aid!r} 不存在", details={"asset_id": aid})
        unique.add(aid)
    return sorted(unique)


def _match_chains(
    outgoing: dict[tuple[str, str], list],
    start: _State,
    query: _State,
) -> list[dict[str, Any]]:
    """从 ``start`` 正向 BFS，返回到达全部命中状态的最短链（按目标排序）。

    命中状态：位于所查 ``(schema, version)`` 且路径与所查路径相等或
    祖先/后代包含。每个状态的父指针在首次发现时确定；邻接状态按定位
    元组升序扩展，因此等长链中保留的是步进定位元组稳定最小者。
    """
    parent: dict[_State, _State | None] = {start: None}
    queue: list[tuple[_State, int]] = [(start, 0)]
    matched: list[_State] = []
    edges_walked = 0
    head = 0
    while head < len(queue):
        state, depth = queue[head]
        head += 1
        if depth >= limits.MAX_IMPACT_DEPTH:
            raise ImpactAnalysisTooLarge(
                "影响链深度超过公开限制",
                details={
                    "reason": "depth_exceeded",
                    "limit": limits.MAX_IMPACT_DEPTH,
                    "schema": state[0],
                    "version": state[1],
                    "path": state[2],
                },
            )
        sp_path = ptr.parse(state[2])
        mapped_states: set[_State] = set()
        for edge in outgoing.get((state[0], state[1]), ()):
            ep = ptr.parse(edge.src_path)
            if not (len(ep) <= len(sp_path) and sp_path[: len(ep)] == ep):
                continue
            edges_walked += 1
            if edges_walked > limits.MAX_IMPACT_VISITED:
                raise ImpactAnalysisTooLarge(
                    "影响链引用边数量超过公开限制",
                    details={
                        "reason": "edges_exceeded",
                        "limit": limits.MAX_IMPACT_VISITED,
                    },
                )
            suffix = sp_path[len(ep) :]
            mapped_states.add(
                (
                    edge.dst_schema,
                    edge.dst_version,
                    ptr.format(list(ptr.parse(edge.dst_path)) + list(suffix)),
                )
            )
        for nxt in sorted(mapped_states):
            if nxt in parent:
                continue
            parent[nxt] = state
            queue.append((nxt, depth + 1))
            if (
                nxt[0] == query[0]
                and nxt[1] == query[1]
                and _related(nxt[2], query[2])
            ):
                matched.append(nxt)

    chains: list[dict[str, Any]] = []
    for target in sorted(matched):
        states: list[_State] = []
        cur: _State | None = target
        while cur is not None:
            states.append(cur)
            cur = parent[cur]
        states.reverse()
        chains.append(
            {
                "source": _locator(states[0]),
                "steps": [
                    {"from": _locator(states[i]), "to": _locator(states[i + 1])}
                    for i in range(len(states) - 1)
                ],
                "target": _locator(states[-1]),
            }
        )
    return chains


def explain_field(
    registry: Registry,
    schema: str,
    version: str,
    path: str,
    asset_ids: list[str] | None,
) -> dict[str, Any]:
    """按资产解释所查字段的影响链路（只读，不改动任何注册内容）。"""
    registry.get_schema(schema, version)  # 未知 Schema/版本抛 NotFoundError
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise NotFoundError(
            f"字段 {schema}@{version}{path!r} 不可达",
            details={"schema": schema, "version": version, "path": path},
        )
    if not registry.logical_path_exists(schema, version, path):
        raise NotFoundError(
            f"字段 {schema}@{version}{path} 不可达",
            details={"schema": schema, "version": version, "path": path},
        )
    selected = _resolve_asset_ids(registry, asset_ids)

    query: _State = (schema, version, path)
    outgoing = _build_outgoing(registry)
    assets = {a.id: a for a in registry.all_assets()}

    entries: list[dict[str, Any]] = []
    for aid in selected:
        asset = assets[aid]
        chains: list[dict[str, Any]] = []
        seen: set[tuple[_State, _State]] = set()
        has_direct = False
        has_transitive = False
        for ref in asset.refs:
            if (ref.schema, ref.version, ref.path) == query:
                # 直接命中：零步链，source 与 target 都是所查字段。
                has_direct = True
                key = (query, query)
                if key not in seen:
                    seen.add(key)
                    chains.append(
                        {
                            "source": _locator(query),
                            "steps": [],
                            "target": _locator(query),
                        }
                    )
                continue
            for chain in _match_chains(
                outgoing, (ref.schema, ref.version, ref.path), query
            ):
                has_transitive = True
                s, t = chain["source"], chain["target"]
                key = (
                    (s["schema"], s["version"], s["path"]),
                    (t["schema"], t["version"], t["path"]),
                )
                if key in seen:
                    continue
                seen.add(key)
                chains.append(chain)
        if not chains:
            continue
        if len(entries) + 1 > limits.MAX_IMPACT_ASSETS:
            raise ImpactAnalysisTooLarge(
                "影响资产数量超过公开限制",
                details={
                    "reason": "assets_exceeded",
                    "limit": limits.MAX_IMPACT_ASSETS,
                },
            )
        chains.sort(
            key=lambda c: (
                c["source"]["schema"],
                c["source"]["version"],
                c["source"]["path"],
                c["target"]["schema"],
                c["target"]["version"],
                c["target"]["path"],
            )
        )
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
                "chains": chains,
            }
        )

    return {
        "schema": schema,
        "version": version,
        "path": path,
        "assets": entries,
    }
