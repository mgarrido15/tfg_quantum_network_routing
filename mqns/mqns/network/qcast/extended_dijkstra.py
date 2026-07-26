import heapq
import math
from typing import Any
from typing import override
from mqns.network.route.route import RouteAlgorithm, RouteQueryResult

class QCastExtendedDijkstra(RouteAlgorithm):
    def __init__(self, q_swap: float = 1.0, default_node_width: int = 1):
        super().__init__("Q-CAST-EDA")
        self.adj = {} 
        self.q_swap = q_swap 
        self.default_node_width = max(1, int(default_node_width))

    def _resolve_node_width(self, node: Any, virtual_widths: dict[Any, int]) -> int:
        if virtual_widths and node in virtual_widths:
            return int(virtual_widths.get(node, 0))

        mem = getattr(node, "memory", None)
        cap = getattr(mem, "capacity", None)
        if cap is not None:
            return int(cap)

        return self.default_node_width

    @override
    def build(self, nodes, channels):
        self.adj = {node: {} for node in nodes}
        for ch in channels:
            u, v = ch.node_list if hasattr(ch, 'node_list') else (ch.node1, ch.node2)
            p = getattr(ch, 'success_prob', 0.99)
            self.adj[u][v] = p
            self.adj[v][u] = p

    def _calcular_ext_y_probabilidades(self, W_actual: int, p_enlace: float, P_array_anterior: list, is_first_hop: bool):
        Q_array = [0.0] * (W_actual + 1)
        P_array_nuevo = [0.0] * (W_actual + 1)
        
        # Calculo Q (Probabilidad de que i fotones tengan exito)
        for i in range(0, W_actual + 1):
            Q_array[i] = math.comb(W_actual, i) * (p_enlace**i) * ((1 - p_enlace)**(W_actual - i))
            
        # Calcular P (Probabilidad acumulada del cuello de botella)
        if is_first_hop:
            # Primer salto:
            for i in range(1, W_actual + 1):
                P_array_nuevo[i] = Q_array[i]
        else:
            W_ant = len(P_array_anterior) - 1
            for i in range(1, W_actual + 1):
                sum_Q = sum(Q_array[l] for l in range(i, W_actual + 1))
                sum_P_ant = sum(P_array_anterior[l] for l in range(i + 1, W_ant + 1)) if i < W_ant else 0.0
                p_ant_i = P_array_anterior[i] if i <= W_ant else 0.0
                P_array_nuevo[i] = (p_ant_i * sum_Q) + (Q_array[i] * sum_P_ant)
                
        # 3. Calcular EXT
        EXT = sum(i * P_array_nuevo[i] for i in range(1, W_actual + 1))
        
        return EXT, P_array_nuevo

    def query(self, src, dst, *args, **kwargs):
        virtual_widths = kwargs.get('virtual_widths', {}) or {}
        has_virtual_widths = bool(virtual_widths)
        src_width = self._resolve_node_width(src, virtual_widths)
        dst_width = self._resolve_node_width(dst, virtual_widths)
        if src_width <= 0 or dst_width <= 0:
            return []

        e_score = {node: -1.0 for node in self.adj}
        prev = {node: None for node in self.adj}
        visited = {node: False for node in self.adj}
        width = {node: 0 for node in self.adj}
        hops = {node: 0 for node in self.adj}
        path_P_array = {node: [] for node in self.adj} 

        pq = [] 
        entry_count = 0 

        e_score[src] = float('inf')
        width[src] = src_width
        path_P_array[src] = []
        
        heapq.heappush(pq, (-e_score[src], entry_count, src))
        entry_count += 1

        while pq:
            curr_e_neg, _, u = heapq.heappop(pq)
            if visited[u]: continue
            visited[u] = True

            if self._resolve_node_width(u, virtual_widths) <= 0:
                continue
            if u == dst:
                metric_final = -curr_e_neg
                return self._reconstruct(prev, src, dst, metric_final)

            for v, p_link in self.adj[u].items():
                cubits_v = self._resolve_node_width(v, virtual_widths)
                if visited[v] or cubits_v <= 0:
                    continue

                # CÁLCULO ASIMÉTRICO DE CÚBITS (El secreto para que funcione bien)
                if v == dst:
                    max_channels_v = cubits_v  # El destino solo recibe (1 cúbit por hilo)
                else:
                    max_channels_v = cubits_v // 2  # Intermedios gastan 2 (recibir y reenviar)

                if max_channels_v <= 0:
                    continue
                
                # El ancho (W) es el cuello de botella entre los nodos
                w_prime = int(min(width[u], max_channels_v))

                if w_prime <= 0:
                    continue
                
                is_first_hop = (u == src)
                
                e_prime_base, P_array_nuevo = self._calcular_ext_y_probabilidades(
                    W_actual=w_prime, 
                    p_enlace=p_link, 
                    P_array_anterior=path_P_array[u], 
                    is_first_hop=is_first_hop
                )
                
                h_prime = hops[u] + 1
                swaps = max(0, h_prime - 1)
                e_prime_with_q = e_prime_base * (self.q_swap ** swaps)

                if e_prime_with_q > e_score[v]:
                    e_score[v] = e_prime_with_q
                    path_P_array[v] = P_array_nuevo  
                    width[v] = w_prime
                    hops[v] = h_prime
                    prev[v] = u
                    
                    heapq.heappush(pq, (-e_prime_with_q, entry_count, v))
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
