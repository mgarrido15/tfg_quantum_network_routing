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
        self.node_capacities: dict[N, int] | None = None

    @override
    def build(self, nodes: list[N], channels: list[C]):
        def _capacity(node: N) -> int:
            if self.node_capacities is not None:
                return int(self.node_capacities.get(node, 0))
            memory = getattr(node, 'memory', None)
            return int(getattr(memory, 'capacity', 1))

        active_nodes = [node for node in nodes if _capacity(node) > 0]
        active_index = {node: idx for idx, node in enumerate(active_nodes)}

        active_channels = []
        for ch in channels:
            assert len(ch.node_list) == 2
            a, b = ch.node_list
            if a in active_index and b in active_index:
                active_channels.append(ch)

        self.route_table.clear()
        for node in nodes:
            self.route_table[node] = {dst: [float('inf'), [dst]] for dst in nodes}

        if not active_nodes:
            return

        # build adjacency matrix
        csr_adj = make_csr(active_nodes, active_channels, self.metric_func)

        # unweighted=True -> hop count; directed=False for undirected topologies
        dist, preds = dijkstra(
            csr_adj,
            directed=False,
            unweighted=self.unweighted,
            return_predecessors=True,
        )

        # Reconstruct path helper
        def _reconstruct_path(src_idx: int, dst_idx: int) -> list[N]:
            # Backtrack from dst to src using predecessors
            path_idx: list[int] = []
            u = dst_idx
            while u not in (-9999, src_idx):
                path_idx.append(u)
                u = preds[src_idx, u]
            path_idx.append(src_idx)
            return [nodes[i] for i in path_idx]

        # For each source node, create the per-destination entry
        for src_idx, src_node in enumerate(active_nodes):
            dest_entry: dict[N, Any] = {}

            for dst_idx, dst_node in enumerate(active_nodes):
                if src_idx == dst_idx:
                    # Source to itself
                    dest_entry[dst_node] = [0.0, [dst_node]]
                    continue

                hop = dist[src_idx, dst_idx]
                if np.isinf(hop):  # Unreachable
                    dest_entry[dst_node] = [np.inf, [dst_node]]
                else:
                    path_nodes = _reconstruct_path(src_idx, dst_idx)
                    dest_entry[dst_node] = [hop, path_nodes]

            self.route_table[src_node].update(dest_entry)

    @override
    def query(self, src: N, dst: N) -> list[RouteQueryResult]:
        ls = self.route_table.get(src, None)
        if ls is None:
            return []
        le = ls.get(dst, None)
        if le is None:
            return []
        try:
            metric, path = le
            path = path.copy()
            path.reverse()
            if len(path) <= 1 or np.isinf(metric):  # unreachable
                next_hop = None
                return []
            else:
                next_hop = path[1]
                return [RouteQueryResult(metric, next_hop, path)]
        except Exception:
            return []
