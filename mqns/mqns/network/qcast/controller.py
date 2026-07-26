import os
import time
from typing import Any, cast
from mqns.entity.cchannel import ClassicPacket
from mqns.network.network.timing import TimingPhaseEvent
from mqns.network.network.reporting import obtener_prob_y_fidelidad_de_ruta
from mqns.network.fw.controller import RoutingController
from mqns.network.fw.routing import RoutingPathStatic
from mqns.network.qcast.extended_dijkstra import QCastExtendedDijkstra
from mqns.utils import log, rng

class QCastController(RoutingController):
    RECOVERY_PRIORITIES = {"metric_only", "hops_then_metric"}

    def __init__(
        self,
        k_max: int = 4,
        enable_recovery_paths: bool = True,
        *,
        replan_each_cycle: bool = True,
        balance_attempts_across_requests: bool = False,
        max_main_path_width: int | None = None,
        cap_success_per_cycle: bool = False,
        max_recovery_paths: int | None = None,
        recovery_priority: str = "metric_only",
        retain_pending_queries_across_cycles: bool = False,
        q_swap: float = 1.0,
    ):
        super().__init__()
        if recovery_priority not in self.RECOVERY_PRIORITIES:
            raise ValueError(
                f"Invalid recovery_priority={recovery_priority!r}. "
                f"Use one of {sorted(self.RECOVERY_PRIORITIES)}"
            )

        self.k_max = k_max
        self.enable_recovery_paths = enable_recovery_paths
        self.replan_each_cycle = replan_each_cycle
        self.balance_attempts_across_requests = balance_attempts_across_requests
        self.max_main_path_width = max_main_path_width
        self.cap_success_per_cycle = cap_success_per_cycle
        self.max_recovery_paths = max_recovery_paths
        self.recovery_priority = recovery_priority
        self.retain_pending_queries_across_cycles = retain_pending_queries_across_cycles
        self.paths = []
        self.pending_qcast_queries = []
        self.eda = QCastExtendedDijkstra(q_swap=q_swap)
        self.net = None
        
        # Estadísticas de ciclo y éxito
        self.current_cycle = 0
        self.query_order_by_cycle = {}
        self.success_history = []
        
        # Diccionarios de estado interno
        self.node_remaining_capacity = {}
        self.successful_requests = 0
        self.request_route_info = {}
        self.request_success = {}
        self.request_success_count = {}
        self.request_fidelities: dict[str, list[float]] = {}
        self.request_install_stats = {}
        self.route_owner_req = {}
        self.route_alias_reqs = {}
        self.route_rr_index = {}
        self.path_w = {}
        self.path_channel_allocations = {}
        self.path_requests: dict[int, list[str]] = {}
        self.main_paths_by_req: dict[str, list[int]] = {}
        self.path_route_names: dict[int, list[str]] = {}
        self.recovery_paths_info = {}
        self.qchannel_activations_by_path: dict[int, int] = {}
        self.qchannel_activation_names_by_path: dict[int, list[str]] = {}
        self.eligible_total = 0
        self.eligible_by_cycle: dict[int, int] = {}
        self.local_entanglement_total = 0
        self.local_entanglement_by_cycle: dict[int, dict[str, Any]] = {}
        self.p4_phase_count = 0
        self.p4_recovery_applied = 0
        self.qcast_route_calc_time_total = 0.0
        self.qcast_route_calc_runs = 0
        self._success_reported_this_cycle: set = set()

    def _cycle_from_time(self, time) -> int:
        return int(round(time.sec / 4.0)) if time is not None and hasattr(time, "sec") else 0

    def record_qchannel_activation(self, path_id: int, qchannel_name: str):
        self.qchannel_activations_by_path[path_id] = self.qchannel_activations_by_path.get(path_id, 0) + 1
        names = self.qchannel_activation_names_by_path.setdefault(path_id, [])
        if qchannel_name not in names:
            names.append(qchannel_name)

    def record_eligible(self):
        self.eligible_total += 1
        cycle = self._cycle_from_time(self.net.simulator.tc) if self.net and getattr(self.net, 'simulator', None) else 0
        self.eligible_by_cycle[cycle] = self.eligible_by_cycle.get(cycle, 0) + 1

    def record_local_entanglement(self, qchannel_name: str, *, success: bool, time) -> None:
        cycle = self._cycle_from_time(time)
        cycle_stats = self.local_entanglement_by_cycle.setdefault(
            cycle,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "channels": {},
            },
        )
        cycle_stats["attempts"] += 1
        cycle_stats["successes" if success else "failures"] += 1
        channel_stats = cycle_stats["channels"].setdefault(
            qchannel_name,
            {"attempts": 0, "successes": 0, "failures": 0},
        )
        channel_stats["attempts"] += 1
        channel_stats["successes" if success else "failures"] += 1
        self.local_entanglement_total += 1

    def record_p4_phase(self):
        self.p4_phase_count += 1

    def record_p4_recovery_applied(self):
        self.p4_recovery_applied += 1

    def install(self, node):
        """Instala el controlador en el nodo maestro de la red"""
        self.node = node
        if hasattr(node, 'apps') and self not in node.apps:
            node.apps.append(self)
        if hasattr(node, 'forwarder'):
            node.forwarder.controller = self
        self.net = self.node.network
        self.next_req_id = getattr(self, 'next_req_id', 0)
        self.next_path_id = getattr(self, 'next_path_id', 0)

    def handle_classic_packet(self, node, msg):
        """FASE P1: Recepción de solicitudes."""
            
        if msg.get("cmd") == "QCAST_QUERY":
            self.pending_qcast_queries.append(msg)
            log.debug(f"QCastController: petición recibida req_id={msg['req_id']} src={msg['src']}")

    def handle(self, event):
        if isinstance(event, TimingPhaseEvent):
            self.handle_sync_phase(event)

    def handle_sync_phase(self, event: TimingPhaseEvent):
        phase_name = str(event.phase).split('.')[-1]
        
        # Ejecutamos el enrutamiento en P2
        if phase_name == "P2":
            self._success_reported_this_cycle.clear()
            if self.replan_each_cycle:
                # Rebuild demand set every cycle so routing is recomputed on fresh traffic.
                self.pending_qcast_queries = self._build_queries_from_network_requests()
            if self.pending_qcast_queries:
                self.current_cycle += 1
                rng.shuffle(self.pending_qcast_queries)
                self.query_order_by_cycle[self.current_cycle] = [req.get("req_id") for req in self.pending_qcast_queries]
                inicio_calculo_rutas = time.perf_counter()
                self._process_all_qcast_requests()
                self.qcast_route_calc_time_total += time.perf_counter() - inicio_calculo_rutas
                self.qcast_route_calc_runs += 1
                if not self.replan_each_cycle and not self.retain_pending_queries_across_cycles:
                    self.pending_qcast_queries.clear()
        
        # Ejecutamos la recuperación de rutas en P4
        elif phase_name == "P4":
            self.record_p4_phase()
            if self.enable_recovery_paths:
                self._handle_p4_swapping_recovery()

    def _deliver_install_path(self, qnode, install_msg):
        if qnode == self.node:
            fw = getattr(qnode, 'forwarder', None)
            if fw is not None and hasattr(fw, 'handle_classic_packet'):
                fw.handle_classic_packet(qnode, install_msg)
            return
        self.node.send_cpacket(qnode, ClassicPacket(install_msg, src=self.node, dest=qnode))

    def _build_fair_m_v(self, route_names: list[str], w: int = 1) -> list[tuple[int, int]]:
        return [(w, w) for _ in range(max(0, len(route_names) - 1))]

    def _route_bottleneck_width(self, route_objs: list[Any]) -> int:
        if not route_objs:
            return 0

        bottleneck = float('inf')
        for i, node in enumerate(route_objs):
            cap = self.node_remaining_capacity.get(node, 0)
            if i != 0 and i != len(route_objs) - 1:
                cap = cap // 2
            bottleneck = min(bottleneck, cap)

        return int(bottleneck) if bottleneck != float('inf') else 0

    def _query_route_without_edges(self, src_node, dst_node, excluded_edges, virtual_widths):
        removed_edges = []
        try:
            for left_node, right_node in excluded_edges:
                if left_node in self.eda.adj and right_node in self.eda.adj[left_node]:
                    removed_edges.append((left_node, right_node, self.eda.adj[left_node][right_node]))
                    del self.eda.adj[left_node][right_node]
                if right_node in self.eda.adj and left_node in self.eda.adj[right_node]:
                    removed_edges.append((right_node, left_node, self.eda.adj[right_node][left_node]))
                    del self.eda.adj[right_node][left_node]
            return self.eda.query(src_node, dst_node, virtual_widths=virtual_widths)
        finally:
            for left_node, right_node, p_link in removed_edges:
                self.eda.adj.setdefault(left_node, {})[right_node] = p_link

    def _initialize_node_capacity_once(self, nodes_list: list[Any]) -> None:
        """Initialize residual capacity once, then keep decrementing it as paths are installed."""
        if self.node_remaining_capacity:
            return

        self.node_remaining_capacity = {
            node: int(getattr(getattr(node, 'memory', None), 'capacity', 0))
            for node in nodes_list
        }

    def _build_queries_from_network_requests(self) -> list[dict[str, str]]:
        if not self.net:
            return []

        requests = []
        for req in getattr(self.net, 'requests', []):
            req_id = req.attr.get('req_id') if hasattr(req, 'attr') and isinstance(req.attr, dict) else None
            if req_id is None:
                continue
            requests.append({
                'cmd': 'QCAST_QUERY',
                'req_id': req_id,
                'src': req.src.name,
                'dst': req.dst.name,
            })
        return requests

    def _process_all_qcast_requests(self):
        """
        FASE P2:
        1) Implementa G-EDA (Greedy Extended Dijkstra) fiel a Q-CAST.
        2) Calcula Rutas de Recuperación (Recovery Paths).
        """
        if not self.net:
            return

        nodes_list = list(getattr(self.net, 'all_nodes', list(getattr(self.net, 'nodes', []))))
        if self.replan_each_cycle:
            # Fresh residual-capacity pool every P2 cycle.
            self.node_remaining_capacity = {}
        self._initialize_node_capacity_once(nodes_list)
        
        qchannels = getattr(self.net, 'qchannels', getattr(self.net, '_qchannels', []))
        self.eda.build(nodes_list, qchannels)

        remaining_queries = list(self.pending_qcast_queries)
        allocated_requests = [] 
        self.recovery_paths_info = {} 

        # ========================================================
        # LÓGICA CORE Q-CAST: UNA RUTA PRINCIPAL POR PETICIÓN
        # ========================================================
        if self.balance_attempts_across_requests:
            # Fair-first mode:
            # 1) Assign width=1 to as many requests as possible.
            # 2) Grow widths in round-robin, bounded by max_main_path_width if configured.
            alloc_buffer: list[list[Any]] = []

            for req in list(remaining_queries):
                src_node = self.net.get_node(req["src"])
                dst_node = self.net.get_node(req["dst"])
                result = self.eda.query(src_node, dst_node, virtual_widths=dict(self.node_remaining_capacity))
                if not result:
                    continue

                best = result[0]
                if self._route_bottleneck_width(best.route) < 1:
                    continue

                if self._consume_route_capacity(best.route, w=1):
                    alloc_buffer.append([req, best, 1])

            growth = True
            while growth:
                growth = False
                for item in alloc_buffer:
                    current_w = item[2]
                    if self.max_main_path_width is not None and current_w >= self.max_main_path_width:
                        continue
                    if self._consume_route_capacity(item[1].route, w=1):
                        item[2] = current_w + 1
                        growth = True

            allocated_requests = [(item[0], item[1], int(item[2])) for item in alloc_buffer]
        else:
            while remaining_queries:
                best_query = None
                best_result = None
                best_ext = -1.0
                best_w = 0

                # Copiamos la lista para poder eliminar elementos si es necesario
                for req in list(remaining_queries):
                    src_node = self.net.get_node(req["src"])
                    dst_node = self.net.get_node(req["dst"])

                    # Buscamos ruta en el grafo residual actual
                    result = self.eda.query(src_node, dst_node, virtual_widths=dict(self.node_remaining_capacity))

                    if result and len(result) > 0:
                        route_objs = result[0].route
                        metric = result[0].metric

                        # Calcular el cuello de botella (bottleneck) de esta ruta
                        w_bottleneck = float('inf')
                        for i, node in enumerate(route_objs):
                            cap = self.node_remaining_capacity.get(node, 0)
                            if i != 0 and i != len(route_objs) - 1:
                                cap = cap // 2
                            w_bottleneck = min(w_bottleneck, cap)

                        w_bottleneck = int(w_bottleneck)
                        if self.max_main_path_width is not None:
                            w_bottleneck = min(w_bottleneck, self.max_main_path_width)

                        # Si hay capacidad física y la métrica es la mejor hasta ahora
                        if w_bottleneck >= 1 and metric > best_ext:
                            best_ext = metric
                            best_result = result[0]
                            best_query = req
                            best_w = w_bottleneck
                    else:
                        # Si no hay ruta posible ni para w=1, eliminamos la petición de este ciclo
                        remaining_queries.remove(req)

                # Si encontramos un ganador en esta iteración del Greedy
                if best_query and best_result and best_w >= 1:
                    # Reservamos la capacidad para actualizar el grafo residual
                    if self._consume_route_capacity(best_result.route, w=best_w):
                        allocated_requests.append((best_query, best_result, best_w))
                        # Una vez asignada la ruta principal, la petición deja de participar en el greedy.
                        remaining_queries.remove(best_query)
                    else:
                        remaining_queries.remove(best_query)
                else:
                    # Si no se encontró ninguna ruta válida para NINGUNA petición restante, la red está llena.
                    break

        # ========================================================
        # INSTALACIÓN Y RECUPERACIÓN (P4)
        # ========================================================
        allocated_req_ids = set()

        for req, result, w_real in sorted(allocated_requests, key=lambda item: (item[1].metric, len(item[1].route))):
            req_id = req["req_id"]
            allocated_req_ids.add(req_id)
            
            src_node = self.net.get_node(req["src"])
            dst_node = self.net.get_node(req["dst"])
            route_objs = result.route
            route_names = [n.name for n in route_objs]
            route_prob, route_fidelity = obtener_prob_y_fidelidad_de_ruta(self.net, route_objs)
            route_hops = len(route_objs) - 1
            recovery_candidates: list[dict[str, Any]] = []
            recovery_routes_seen: set[tuple[str, ...]] = set()

            if req_id not in self.request_route_info:
                self.request_route_info[req_id] = {
                    'src': src_node.name, 'dst': dst_node.name,
                    'route': route_names, 'hops': route_hops, 'metric': result.metric,
                    'route_success_prob': route_prob, 'route_fidelity': route_fidelity,
                    'w_asignado': 0, 'multi_routes': []
                }
            
            # Siempre actualiza los campos principales de la ruta con la ruta actual que se está procesando.
            self.request_route_info[req_id]['route'] = route_names
            self.request_route_info[req_id]['hops'] = route_hops
            self.request_route_info[req_id]['metric'] = result.metric
            self.request_route_info[req_id]['route_success_prob'] = route_prob
            self.request_route_info[req_id]['route_fidelity'] = route_fidelity
            self.request_route_info[req_id]['w_asignado'] = w_real
            self.request_route_info[req_id]['multi_routes'] = [{'route': route_names, 'w': w_real}]
            self.request_success.setdefault(req_id, False)

            # Generación e instalación en FIB
            path_id = self.next_path_id
            self.next_path_id += 1
            self.path_w[path_id] = w_real
            self.path_route_names[path_id] = route_names
            self.main_paths_by_req.setdefault(req_id, []).append(path_id)
            
            route_path = RoutingPathStatic(route_names, req_id=0, path_id=path_id, m_v=self._build_fair_m_v(route_names, w=w_real))
            instructions = next(route_path.compute_paths(self.net))
            instructions["req_id"] = req_id
            install_msg = {"cmd": "INSTALL_PATH", "path_id": path_id, "instructions": instructions}
            
            for node_name in route_names:
                self._deliver_install_path(self.net.get_node(node_name), install_msg)
            
            self.path_requests[path_id] = [req_id]

            # CÁLCULO RUTAS DE RECUPERACIÓN (P4 Q-CAST)
            self.recovery_paths_info[path_id] = []
            if not self.enable_recovery_paths:
                continue
            
            # --- 1. CREACIÓN DEL GRAFO RESIDUAL ---
            # Hacemos una copia de la memoria disponible justo después de instalar la ruta principal
            memoria_residual = dict(self.node_remaining_capacity)
            
            h = len(route_objs)
            for l in range(1, min(self.k_max, h)):
                for idx in range(h - l):
                    u = route_objs[idx]     
                    v = route_objs[idx + l] 
                    
                    direct_result = self.eda.query(u, v, virtual_widths=memoria_residual)
                    if direct_result and len(direct_result) > 0:
                        direct_route = direct_result[0].route
                        direct_names = [n.name for n in direct_route]
                        segmento_original = [node.name for node in route_objs[idx:idx+l+1]]
                        if direct_names != segmento_original:
                            route_key = tuple(direct_names)
                            if route_key not in recovery_routes_seen:
                                recovery_routes_seen.add(route_key)
                                recovery_candidates.append({
                                    'segment_src': u.name,
                                    'segment_dst': v.name,
                                    'route': direct_names,
                                    'metric': direct_result[0].metric,
                                    'width': self._route_bottleneck_width(direct_route),
                                    'hops': len(direct_names) - 1,
                                })

                    excluded_edge_names = {
                        tuple(sorted((left_name, right_name)))
                        for route_id, route_names in self.path_route_names.items()
                        if route_names
                        for left_name, right_name in zip(route_names[:-1], route_names[1:])
                    }
                    excluded_edge_names.update(
                        tuple(sorted((left_node.name, right_node.name)))
                        for left_node, right_node in zip(route_objs[idx:idx + l], route_objs[idx + 1:idx + l + 1])
                    )
                    excluded_edges = [
                        (self.net.get_node(left_name), self.net.get_node(right_name))
                        for left_name, right_name in excluded_edge_names
                        if self.net.get_node(left_name) is not None and self.net.get_node(right_name) is not None
                    ]

                    excluded_result = self._query_route_without_edges(
                        u,
                        v,
                        excluded_edges,
                        memoria_residual,
                    )
                    if excluded_result and len(excluded_result) > 0:
                        excluded_route = excluded_result[0].route
                        excluded_names = [n.name for n in excluded_route]
                        segmento_original = [node.name for node in route_objs[idx:idx+l+1]]
                        if excluded_names != segmento_original:
                            route_key = tuple(excluded_names)
                            if route_key not in recovery_routes_seen:
                                recovery_routes_seen.add(route_key)
                                recovery_candidates.append({
                                    'segment_src': u.name,
                                    'segment_dst': v.name,
                                    'route': excluded_names,
                                    'metric': excluded_result[0].metric,
                                    'width': self._route_bottleneck_width(excluded_route),
                                    'hops': len(excluded_names) - 1,
                                })

            if self.recovery_priority == "metric_only":
                recovery_candidates.sort(key=lambda item: (-item['metric'], item['route']))
            else:
                recovery_candidates.sort(key=lambda item: (item['hops'], -item['metric'], item['route']))
            selected_recovery_candidates: list[dict[str, Any]] = []
            selected_routes: set[tuple[str, ...]] = set()
            selected_segments: set[tuple[str, str]] = set()
            for candidate in recovery_candidates:
                route_key = tuple(candidate['route'])
                segment_key = tuple(sorted((candidate['segment_src'], candidate['segment_dst'])))
                if route_key in selected_routes or segment_key in selected_segments:
                    continue
                selected_routes.add(route_key)
                selected_segments.add(segment_key)
                selected_recovery_candidates.append(candidate)
                if self.max_recovery_paths is not None and len(selected_recovery_candidates) >= self.max_recovery_paths:
                    break

            for candidate in selected_recovery_candidates:
                self._install_recovery_candidate(
                    owner_req_id=req_id,
                    main_path_id=path_id,
                    candidate=candidate,
                )

            if not self.recovery_paths_info[path_id] and len(route_objs) > 2:
                sd_virtual_widths = {
                    node: getattr(getattr(node, 'memory', None), 'capacity', 0)
                    for node in nodes_list
                }
                for node in route_objs[1:-1]:
                    sd_virtual_widths[node] = 0

                alt_result = self.eda.query(route_objs[0], route_objs[-1], virtual_widths=sd_virtual_widths)
                if alt_result and len(alt_result) > 0:
                    alt_route = alt_result[0].route
                    alt_names = [n.name for n in alt_route]
                    if alt_names != route_names:
                        self._install_recovery_candidate(
                            owner_req_id=req_id,
                            main_path_id=path_id,
                            candidate={
                                'segment_src': route_objs[0].name,
                                'segment_dst': route_objs[-1].name,
                                'route': alt_names,
                                'metric': alt_result[0].metric,
                                'width': self._route_bottleneck_width(alt_route),
                            },
                        )

        # Registro de solicitudes rechazadas o encoladas
        todavia_pendientes = []
        for req in self.pending_qcast_queries:
            req_id = req["req_id"]
            if req_id not in allocated_req_ids:
                if req_id not in self.request_route_info:
                    self.request_route_info[req_id] = {
                        'src': req["src"], 'dst': req["dst"], 'route': None, 'hops': 0, 'metric': 0.0,
                        'route_success_prob': 0.0, 'route_fidelity': 0.0, 'w_asignado': 0, 'multi_routes': []
                    }
                self.request_success.setdefault(req_id, False)
                if self.retain_pending_queries_across_cycles:
                    todavia_pendientes.append(req)

        # Si está habilitada la cola persistente, dejamos solo las no asignadas.
        if (not self.replan_each_cycle) and self.retain_pending_queries_across_cycles:
            self.pending_qcast_queries = todavia_pendientes

    def _consume_route_capacity(self, route_objs, w: int = 1) -> bool:
        if len(route_objs) < 2 or w <= 0: return False
        
        # 1. Chequeo
        for i, node in enumerate(route_objs):
            required = w if (i == 0 or i == len(route_objs) - 1) else (2 * w)
            if self.node_remaining_capacity.get(node, 0) < required:
                return False 

        # 2. Consumo
        for i, node in enumerate(route_objs):
            consume = w if (i == 0 or i == len(route_objs) - 1) else (2 * w)
            self.node_remaining_capacity[node] -= consume
            
        return True

    def _install_recovery_candidate(self, *, owner_req_id: str, main_path_id: int, candidate: dict[str, Any]) -> bool:
        rec_w = int(candidate.get('width', 0))
        alt_names = list(candidate.get('route', []))
        if rec_w <= 0 or not alt_names:
            return False

        net = self.net
        if net is None:
            return False

        alt_nodes = [net.get_node(name) for name in alt_names]
        if any(node is None for node in alt_nodes):
            return False

        if not self._consume_route_capacity(alt_nodes, w=rec_w):
            log.debug(
                f"Q-CAST recovery path skipped due to insufficient residual capacity: "
                f"req_id={owner_req_id} segment={candidate.get('segment_src')}-{candidate.get('segment_dst')} "
                f"route={alt_names} w={rec_w}"
            )
            return False

        rec_path_id = self.next_path_id
        self.next_path_id += 1
        self.path_w[rec_path_id] = rec_w
        self.path_route_names[rec_path_id] = alt_names
        rec_req_id = f"{owner_req_id}__REC_{rec_path_id}"

        rec_route_path = RoutingPathStatic(
            alt_names,
            req_id=rec_path_id,
            path_id=rec_path_id,
            m_v=self._build_fair_m_v(alt_names, w=rec_w),
        )
        rec_instructions = next(rec_route_path.compute_paths(net))
        cast(Any, rec_instructions)["req_id"] = rec_req_id
        rec_install_msg = {"cmd": "INSTALL_PATH", "path_id": rec_path_id, "instructions": rec_instructions}

        for node_name in alt_names:
            self._deliver_install_path(net.get_node(node_name), rec_install_msg)

        self.path_requests[rec_path_id] = [owner_req_id, rec_req_id]
        self.recovery_paths_info[main_path_id].append({
            'segment_src': candidate.get('segment_src'),
            'segment_dst': candidate.get('segment_dst'),
            'route': alt_names,
            'metric': candidate.get('metric'),
            'hops': candidate.get('hops', max(0, len(alt_names) - 1)),
            'w': rec_w,
            'rec_path_id': rec_path_id,
        })
        return True

    def report_success(self, req_id, time, fidelity: float | None = None):
        """Report E2E entanglement success.

        When cap_success_per_cycle is True, only one success per req_id per cycle
        is counted. By default, every success is counted.
        """
        try:
            if self.cap_success_per_cycle:
                if req_id in self._success_reported_this_cycle:
                    return
                self._success_reported_this_cycle.add(req_id)
            self.successful_requests += 1
            self.request_success[req_id] = True
            self.request_success_count[req_id] = self.request_success_count.get(req_id, 0) + 1
            if fidelity is not None:
                self.request_fidelities.setdefault(req_id, []).append(float(fidelity))
            self.success_history.append({
                "cycle": self.current_cycle,
                "req_id": req_id,
                "fidelity": fidelity if fidelity is not None else None,
            })
        except Exception as e:
            log.error(f"Error counting success: {e}")

    def _handle_p4_swapping_recovery(self):
        for req_id, info in self.request_route_info.items():
            main_path_ids = self.main_paths_by_req.get(req_id, [])
            if not main_path_ids:
                continue

            for path_id in main_path_ids:
                route_names = self.path_route_names.get(path_id, info.get('route') or [])
                if not route_names:
                    continue

                recoveries = self.recovery_paths_info.get(path_id, [])
                if not recoveries:
                    continue

                broken_segments = []
                for i in range(len(route_names) - 1):
                    u_name, v_name = route_names[i], route_names[i + 1]
                    if not self._check_segment_entangled(u_name, v_name, path_id):
                        broken_segments.append((u_name, v_name))

                if not broken_segments:
                    continue

                repaired_segments = set()
                for br_u, br_v in broken_segments:
                    if (br_u, br_v) in repaired_segments:
                        continue

                    best_patch = self._select_best_recovery_patch(br_u, br_v, recoveries)

                    if best_patch:
                        repaired_segments.add((br_u, br_v))
                        self.record_p4_recovery_applied()
                        log.info(f"Q-CAST P4 REPARADO: Fallo en {br_u}-{br_v}. Usando desvío: {best_patch['route']}")
                        self._apply_patch_swapping(path_id, best_patch)

    def _select_best_recovery_patch(self, br_u, br_v, recoveries):
        matching_patches = []
        fallback_patches = []

        for rec in recoveries:
            rec_path_id = rec['rec_path_id']
            patch_ready = all(
                self._check_segment_entangled(rec['route'][j], rec['route'][j + 1], rec_path_id)
                for j in range(len(rec['route']) - 1)
            )
            if not patch_ready:
                continue

            if self._patch_covers_segment(br_u, br_v, rec):
                matching_patches.append(rec)
            else:
                fallback_patches.append(rec)

        def patch_rank(patch):
            if self.recovery_priority == "metric_only":
                return (-float(patch.get('metric', 0.0)), tuple(patch.get('route', [])))
            return (
                int(patch.get('hops', max(0, len(patch.get('route', [])) - 1))),
                -float(patch.get('metric', 0.0)),
                tuple(patch.get('route', [])),
            )

        if matching_patches:
            return min(matching_patches, key=patch_rank)
        if fallback_patches:
            return min(fallback_patches, key=patch_rank)
        return None

    def _patch_covers_segment(self, u_name, v_name, patch):
        if patch['segment_src'] == u_name and patch['segment_dst'] == v_name:
            return True

        route = patch.get('route', [])
        if u_name not in route or v_name not in route:
            return False
        return route.index(u_name) < route.index(v_name)
                    
    def _check_segment_entangled(self, u_name, v_name, path_id):
        if not self.net: return False
        u = self.net.get_node(u_name)
        canales = self.net.get_qchannels_between(u_name, v_name)
        if not canales: return False
        for q in getattr(u.memory, 'qubits', []):
            if getattr(q, 'path_id', None) == path_id and getattr(q, 'qchannel', None) in canales:
                if q.state.name in ["ENTANGLED0", "ENTANGLED1", "ENTANGLED2", "ENTANGLED", "ELIGIBLE"]:
                    return True
        return False
        
    def _apply_patch_swapping(self, main_path_id, patch):
        if not self.net: return False
        for step_node in patch['route']:
            qn = self.net.get_node(step_node)
            forwarder = getattr(qn, 'forwarder', None) 
            if forwarder and hasattr(forwarder, 'attempt_swapping'):
                for q in getattr(qn.memory, 'qubits', []):
                    if getattr(q, 'path_id', None) == patch['rec_path_id']:
                        q.path_id = main_path_id
                        if q.state.name.startswith("ENTANGLED"):
                            forwarder.attempt_swapping(q)
class QCastMultiEntController(QCastController):
    """Q-CAST variant that always counts every E2E entanglement.

    This class is kept for backward compatibility in experiments where
    ``QCastController`` could be configured with ``cap_success_per_cycle=True``.
    ``QCastMultiEntController`` ignores that cap and always counts all successes.
    """

    def report_success(self, req_id, time, fidelity: float | None = None):
        """Report every E2E entanglement success without cap."""
        try:
            self.successful_requests += 1
            self.request_success[req_id] = True
            self.request_success_count[req_id] = self.request_success_count.get(req_id, 0) + 1
            if fidelity is not None:
                self.request_fidelities.setdefault(req_id, []).append(float(fidelity))
            self.success_history.append({
                "cycle": self.current_cycle,
                "req_id": req_id,
                "fidelity": fidelity if fidelity is not None else None,
            })
        except Exception as e:
            log.error(f"Error counting success: {e}")
