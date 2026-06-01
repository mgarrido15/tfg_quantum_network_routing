import heapq
import math
from typing import override
from mqns.network.route.route import RouteAlgorithm, RouteQueryResult

class QCastExtendedDijkstra(RouteAlgorithm):
    def __init__(self, q_swap: float = 1.0):
        super().__init__("Q-CAST-EDA")
        self.adj = {} 
        self.q_swap = q_swap 

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
        if virtual_widths and (virtual_widths.get(src, 0) <= 0 or virtual_widths.get(dst, 0) <= 0):
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
        width[src] = virtual_widths.get(src, float('inf'))
        path_P_array[src] = []
        
        heapq.heappush(pq, (-e_score[src], entry_count, src))
        entry_count += 1

        while pq:
            curr_e_neg, _, u = heapq.heappop(pq)
            if visited[u]: continue
            visited[u] = True

            if virtual_widths.get(u, 0) <= 0:
                continue
            if u == dst:
                metric_final = -curr_e_neg
                return self._reconstruct(prev, src, dst, metric_final)

            for v, p_link in self.adj[u].items():
                cubits_v = virtual_widths.get(v, 0)
                if visited[v] or virtual_widths.get(v, 0) <= 0: 
                    continue

                # CÁLCULO ASIMÉTRICO DE CÚBITS (El secreto para que funcione bien)
                if v == dst:
                    max_channels_v = cubits_v  # El destino solo recibe (1 cúbit por hilo)
                else:
                    max_channels_v = cubits_v // 2  # Intermedios gastan 2 (recibir y reenviar)

                if max_channels_v <= 0:
                    continue
                
                # El ancho (W) es el cuello de botella entre los nodos
                w_prime = min(width[u], max_channels_v)

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
    

class QCastExtendedDijkstraFidelity(RouteAlgorithm):

    """Q-CAST variant that includes route fidelity in the routing metric."""

    def __init__(self):
        super().__init__("Q-CAST-EDA-FIDELITY")
        self.adj = {}
        self.fid_adj = {}
        self.node_fidelity = {}



    @override

    def build(self, nodes, channels):
        self.adj = {node: {} for node in nodes}
        self.fid_adj = {node: {} for node in nodes}
        self.node_fidelity = {node: float(getattr(node, "node_fidelity", 1.0)) for node in nodes}

        for ch in channels:
            u, v = ch.node_list if hasattr(ch, 'node_list') else (ch.node1, ch.node2)
            p = getattr(ch, 'success_prob', 1.0)
            fid = getattr(ch, "_fidelity", None)
            if fid is None:
                transfer_error = getattr(ch, "transfer_error", None)
                if transfer_error is not None and hasattr(transfer_error, "p_survival"):
                    fid = (3 * transfer_error.p_survival + 1) / 4
                else:
                    fid = 0.99
            self.adj[u][v] = p
            self.adj[v][u] = p
            self.fid_adj[u][v] = float(fid)
            self.fid_adj[v][u] = float(fid)

    def query(self, src, dst, *args, **kwargs):

        """Query route considering probability and fidelity. Metric: width * prob * fidelity"""

        virtual_widths = kwargs.get('virtual_widths', {}) or {}
        if virtual_widths and (virtual_widths.get(src, 0) <= 0 or virtual_widths.get(dst, 0) <= 0):
            return []

        e_score = {node: -1.0 for node in self.adj}
        prev = {node: None for node in self.adj}
        visited = {node: False for node in self.adj}
        path_prob = {node: 0.0 for node in self.adj}
        path_fidelity = {node: 0.0 for node in self.adj}
        width = {node: 0 for node in self.adj}
        pq = []
        entry_count = 0
        e_score[src] = float('inf')
        path_prob[src] = 1.0
        path_fidelity[src] = self.node_fidelity.get(src, 1.0)
        width[src] = virtual_widths[src]
        heapq.heappush(pq, (-e_score[src], entry_count, src))
        entry_count += 1

        while pq:
            curr_e_neg, _, u = heapq.heappop(pq)
            if visited[u]: continue
            visited[u] = True
            if virtual_widths.get(u, 0) <= 0: continue
            if u == dst: return self._reconstruct(prev, src, dst, -curr_e_neg)

            for v, p_link in self.adj[u].items():
                if visited[v] or virtual_widths[v] <= 0: continue
                w_prime = min(width[u], virtual_widths[v])
                p_prime = path_prob[u] * p_link
                f_link = self.fid_adj[u][v]
                f_node = self.node_fidelity.get(v, 1.0)
                f_prime = path_fidelity[u] * f_link * f_node
                e_prime = w_prime * p_prime * f_prime
                if e_prime > e_score[v]:
                    e_score[v] = e_prime
                    path_prob[v] = p_prime
                    path_fidelity[v] = f_prime
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