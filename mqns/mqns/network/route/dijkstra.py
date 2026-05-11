#    SimQN: a discrete-event simulator for the quantum networks
#    Copyright (C) 2021-2022 Lutong Chen, Jian Li, Kaiping Xue
#    University of Science and Technology of China, USTC.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.

from typing import Any, override

import numpy as np
from scipy.sparse.csgraph import dijkstra

from mqns.entity.base_channel import BaseChannel
from mqns.entity.node import Node
from mqns.network.route.route import MetricFunc, RouteAlgorithm, RouteQueryResult, make_csr


def _active_nodes_by_capacity[N: Node](nodes: list[N]) -> list[N]:
    active_nodes: list[N] = []
    for node in nodes:
        memory = getattr(node, "memory", None)
        capacity = int(getattr(memory, "capacity", 1))
        if capacity > 0:
            active_nodes.append(node)
    return active_nodes


def _build_dijkstra_route_table[N: Node, C: BaseChannel](
    route_table: dict[N, dict[N, tuple[float, list[N]]]],
    nodes: list[N],
    channels: list[C],
    metric_func: MetricFunc[C],
    active_nodes: list[N],
    unweighted: bool,
) -> None:
    active_index = {node: idx for idx, node in enumerate(active_nodes)}

    active_channels: list[C] = []
    for ch in channels:
        assert len(ch.node_list) == 2
        a, b = ch.node_list
        if a in active_index and b in active_index:
            active_channels.append(ch)

    route_table.clear()
    for node in nodes:
        route_table[node] = {dst: (float("inf"), [dst]) for dst in nodes}

    if not active_nodes:
        return

    csr_adj = make_csr(active_nodes, active_channels, metric_func)

    dist, preds = dijkstra(
        csr_adj,
        directed=False,
        unweighted=unweighted,
        return_predecessors=True,
    )

    def _reconstruct_path(src_idx: int, dst_idx: int) -> list[N]:
        path_idx: list[int] = []
        u = dst_idx
        while u not in (-9999, src_idx):
            path_idx.append(u)
            u = preds[src_idx, u]
        path_idx.append(src_idx)
        return [active_nodes[i] for i in path_idx]

    for src_idx, src_node in enumerate(active_nodes):
        dest_entry: dict[N, Any] = {}

        for dst_idx, dst_node in enumerate(active_nodes):
            if src_idx == dst_idx:
                dest_entry[dst_node] = (0.0, [dst_node])
                continue

            hop = dist[src_idx, dst_idx]
            if np.isinf(hop):
                dest_entry[dst_node] = (np.inf, [dst_node])
            else:
                dest_entry[dst_node] = (float(hop), _reconstruct_path(src_idx, dst_idx))

        route_table[src_node].update(dest_entry)


def _query_dijkstra_route_table[N: Node](
    route_table: dict[N, dict[N, tuple[float, list[N]]]],
    src: N,
    dst: N,
) -> list[RouteQueryResult[N]]:
    ls = route_table.get(src, None)
    if ls is None:
        return []
    le = ls.get(dst, None)
    if le is None:
        return []
    try:
        metric, path = le
        path = path.copy()
        path.reverse()
        if len(path) <= 1 or np.isinf(metric):
            return []
        return [RouteQueryResult(metric, path[1], path)]
    except Exception:
        return []


class DijkstraRouteAlgorithm[N: Node, C: BaseChannel](RouteAlgorithm[N, C]):
    """
    Dijkstra algorithm.

    This is implemented with SciPy's csgraph Dijkstra on a CSR adjacency.
    """

    @override
    def __init__(self, name="dijkstra", metric_func: MetricFunc[C] | None = None) -> None:
        """
        Args:
            name: Name of the routing algorithm (default: "dijkstra").
            metric_func: Function returning the metric (weight) for each channel.
                Defaults to a constant function m(l) = 1.
        """
        super().__init__(name, metric_func)
        self.route_table: dict[N, dict[N, tuple[float, list[N]]]] = {}

    @override
    def build(self, nodes: list[N], channels: list[C]):
        _build_dijkstra_route_table(
            self.route_table,
            nodes,
            channels,
            self.metric_func,
            nodes,
            self.unweighted,
        )

    @override
    def query(self, src: N, dst: N) -> list[RouteQueryResult]:
        return _query_dijkstra_route_table(self.route_table, src, dst)


class DijkstraCapacityRouteAlgorithm[N: Node, C: BaseChannel](RouteAlgorithm[N, C]):
    """
    Dijkstra variant that filters nodes by memory capacity.
    """

    @override
    def __init__(self, name="dijkstra_capacity", metric_func: MetricFunc[C] | None = None) -> None:
        super().__init__(name, metric_func)
        self.route_table: dict[N, dict[N, tuple[float, list[N]]]] = {}

    @override
    def build(self, nodes: list[N], channels: list[C]):
        active_nodes = _active_nodes_by_capacity(nodes)
        _build_dijkstra_route_table(
            self.route_table,
            nodes,
            channels,
            self.metric_func,
            active_nodes,
            self.unweighted,
        )

    @override
    def query(self, src: N, dst: N) -> list[RouteQueryResult[N]]:
        return _query_dijkstra_route_table(self.route_table, src, dst)


class DijkstraDistanceRouteAlgorithm[N: Node, C: BaseChannel](RouteAlgorithm[N, C]):
    """
    Dijkstra variant that uses physical link length as the edge metric.

    This variant also filters nodes by memory capacity before building routes.
    """

    @override
    def __init__(self, name="dijkstra_distance") -> None:
        super().__init__(name, lambda ch: float(ch.length))
        self.route_table: dict[N, dict[N, tuple[float, list[N]]]] = {}

    @override
    def build(self, nodes: list[N], channels: list[C]):
        active_nodes = _active_nodes_by_capacity(nodes)
        _build_dijkstra_route_table(
            self.route_table,
            nodes,
            channels,
            self.metric_func,
            active_nodes,
            self.unweighted,
        )

    @override
    def query(self, src: N, dst: N) -> list[RouteQueryResult[N]]:
        return _query_dijkstra_route_table(self.route_table, src, dst)