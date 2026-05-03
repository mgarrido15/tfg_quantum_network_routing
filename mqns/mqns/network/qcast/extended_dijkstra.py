import heapq
from typing import override
from mqns.network.route.route import RouteAlgorithm, RouteQueryResult

class QCastExtendedDijkstra(RouteAlgorithm):
    def __init__(self):
        super().__init__("Q-CAST-EDA")
        self.adj = {} 

    @override
    def build(self, nodes, channels):
        self.adj = {node: {} for node in nodes}
        for ch in channels:
            u, v = ch.node_list if hasattr(ch, 'node_list') else (ch.node1, ch.node2)
            p = getattr(ch, 'success_prob', 1.0)
            self.adj[u][v] = p
            self.adj[v][u] = p

    def query(self, src, dst, *args, **kwargs):
        """Query route from src to dst.

        Accepts an optional keyword `virtual_widths` (dict[node->int]) to enforce
        node capacity constraints. Kept signature flexible to remain compatible
        with the base `RouteAlgorithm.query(src, dst)`.
        """
        virtual_widths = kwargs.get('virtual_widths', {}) or {}
        if virtual_widths and (virtual_widths.get(src, 0) <= 0 or virtual_widths.get(dst, 0) <= 0):
            return []

        e_score = {node: -1.0 for node in self.adj}
        prev = {node: None for node in self.adj}
        visited = {node: False for node in self.adj}
        path_prob = {node: 0.0 for node in self.adj}
        width = {node: 0 for node in self.adj}

        pq = [] 
        entry_count = 0 

        e_score[src] = float('inf')
        path_prob[src] = 1.0
        width[src] = virtual_widths[src]
        
        heapq.heappush(pq, (-e_score[src], entry_count, src))
        entry_count += 1

        while pq:
            curr_e_neg, _, u = heapq.heappop(pq)
            if visited[u]: continue
            visited[u] = True

            if virtual_widths.get(u, 0) <= 0:
                continue

            if u == dst:
                return self._reconstruct(prev, src, dst, -curr_e_neg)

            for v, p_link in self.adj[u].items():
                if visited[v] or virtual_widths[v] <= 0: continue

                w_prime = min(width[u], virtual_widths[v])
                p_prime = path_prob[u] * p_link
                e_prime = w_prime * p_prime

                if e_prime > e_score[v]:
                    e_score[v] = e_prime
                    path_prob[v] = p_prime
                    width[v] = w_prime
                    prev[v] = u
                    heapq.heappush(pq, (-e_prime, entry_count, v))
                    entry_count += 1
        return []

    def _reconstruct(self, prev, src, dst, metric):
        path = []
        curr = dst
        while curr is not None:
            path.append(curr)
            curr = prev[curr]
        path.reverse()
        if len(path) < 2: return []
        return [RouteQueryResult(metric=metric, next_hop=path[1], route=path)]