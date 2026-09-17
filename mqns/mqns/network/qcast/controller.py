import copy
import random
import time
from collections.abc import Callable
from typing import Any, cast
from mqns.entity.cchannel import ClassicPacket
from mqns.network.network.timing import TimingPhaseEvent
from mqns.network.network.reporting import obtener_prob_y_fidelidad_de_ruta
from mqns.network.fw.controller import RoutingController
from mqns.network.fw.routing import RoutingPathStatic
from mqns.network.fw.swap_sequence import SwapSequenceInput
from mqns.network.qcast.extended_dijkstra import QCastExtendedDijkstra
from mqns.utils import log

class QCastController(RoutingController):
    count_multiple_e2e_per_cycle = False

    def __init__(
        self,
        k_max: int = 4,
        enable_recovery_paths: bool = True,
        max_alloc_width: int | None = None,
        swap_policy: SwapSequenceInput = "asap",
        q_swap: float = 1.0,
        collect_validation_history: bool = False,
        priority_seed: int = 0,
    ):
        super().__init__()
        self.k_max = k_max
        self.enable_recovery_paths = enable_recovery_paths
        self.max_alloc_width = max_alloc_width
        self.swap_policy: SwapSequenceInput = swap_policy
        self.collect_validation_history = collect_validation_history
        self.priority_seed = priority_seed
        self.priority_rng = random.Random(priority_seed)
        self.paths = []
        self.pending_qcast_queries = []
        self.eda = QCastExtendedDijkstra(q_swap=q_swap)
        self.q_swap = q_swap
        self.net = None
        
        # Estadísticas de ciclo y éxito
        self.current_cycle = 0
        self.query_order_by_cycle = {}
        self.success_history = []
        
        # Diccionarios de estado interno
        self.node_remaining_capacity = {}
        self.edge_remaining_capacity: dict[tuple[str, str], int] = {}
        self.successful_requests = 0
        self.request_route_info = {}
        self.request_success = {}
        self.request_success_count = {}
        self.request_fidelities: dict[str, list[float]] = {}
        self.e2e_candidate_history: list[dict[str, Any]] = []
        self.e2e_completion_history: list[dict[str, Any]] = []
        self.swap_history: list[dict[str, Any]] = []
        self.invalid_e2e_endpoint_count = 0
        self.orphaned_e2e_count = 0
        self.request_install_stats = {}
        self.route_owner_req = {}
        self.route_alias_reqs = {}
        self.route_rr_index = {}
        self.path_w = {}
        self.path_channel_allocations: dict[int, dict[str, list[str]]] = {}
        self.path_channel_allocation_history: dict[int, dict[str, list[str]]] = {}
        self.path_requests: dict[int, list[str]] = {}
        self.path_request_history: dict[int, list[str]] = {}
        self.main_paths_by_req: dict[str, list[int]] = {}
        self.path_route_names: dict[int, list[str]] = {}
        self.path_route_history: dict[int, list[str]] = {}
        self.recovery_paths_info = {}
        self.recovery_path_history: dict[int, dict[str, Any]] = {}
        self.qchannel_activations_by_path: dict[int, int] = {}
        self.qchannel_activation_names_by_path: dict[int, list[str]] = {}
        self.eligible_total = 0
        self.eligible_by_cycle: dict[int, int] = {}
        self.local_entanglement_total = 0
        self.local_entanglement_by_cycle: dict[int, dict[str, Any]] = {}
        self.local_attempts_by_request: dict[str, int] = {}
        self.local_successes_by_request: dict[str, int] = {}
        self.p4_phase_count = 0
        self.p4_recovery_applied = 0
        self.qcast_route_calc_time_total = 0.0
        self.qcast_route_calc_runs = 0
        self._success_reported_this_cycle: set = set()
        self.manage_routes_each_cycle = False
        self.active_cycle_requests: list[dict[str, str]] = []
        self.cycle_route_callback: Callable[[list[dict[str, str]]], None] | None = None
        self.request_route_history: dict[int, dict[str, dict[str, Any]]] = {}

    def _cycle_from_time(self, time) -> int:
        return int(round(time.sec / 4.0)) if time is not None and hasattr(time, "sec") else 0

    def record_qchannel_activation(self, path_id: int, qchannel_name: str):
        self.qchannel_activations_by_path[path_id] = self.qchannel_activations_by_path.get(path_id, 0) + 1
        names = self.qchannel_activation_names_by_path.setdefault(path_id, [])
        if qchannel_name not in names:
            names.append(qchannel_name)

    def record_eligible(self):
        self.eligible_total += 1
        cycle = self.current_cycle
        self.eligible_by_cycle[cycle] = self.eligible_by_cycle.get(cycle, 0) + 1

    def record_local_entanglement(
        self,
        qchannel_name: str,
        *,
        success: bool,
        time,
        path_id: int | None = None,
    ) -> None:
        _ = time
        cycle = self.current_cycle
        cycle_stats = self.local_entanglement_by_cycle.setdefault(
            cycle,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "channels": {},
                "paths": {},
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
        if self.collect_validation_history:
            path_stats = cycle_stats["paths"].setdefault(
                str(path_id),
                {"attempts": 0, "successes": 0, "failures": 0},
            )
            path_stats["attempts"] += 1
            path_stats["successes" if success else "failures"] += 1
        request_ids = self.path_requests.get(path_id, []) if path_id is not None else []
        for path_req_id in request_ids:
            base_req_id = str(path_req_id).split("__BACKUP_", 1)[0]
            self.local_attempts_by_request[base_req_id] = (
                self.local_attempts_by_request.get(base_req_id, 0) + 1
            )
            if success:
                self.local_successes_by_request[base_req_id] = (
                    self.local_successes_by_request.get(base_req_id, 0) + 1
                )
        self.local_entanglement_total += 1

    def record_e2e_candidate(
        self,
        *,
        req_id: str,
        path_id: int,
        epr_name: str,
        endpoints: tuple[str | None, str | None],
        elementary_eprs: list[dict[str, Any]],
    ) -> None:
        if not self.collect_validation_history:
            return
        self.e2e_candidate_history.append({
            "cycle": self.current_cycle,
            "req_id": req_id,
            "path_id": path_id,
            "epr_name": epr_name,
            "endpoints": list(endpoints),
            "elementary_eprs": elementary_eprs,
        })

    def record_swap(
        self,
        *,
        req_id: str | int,
        path_id: int,
        node: str,
        success: bool,
        left_endpoints: tuple[str | None, str | None],
        right_endpoints: tuple[str | None, str | None],
    ) -> None:
        if not self.collect_validation_history:
            return
        self.swap_history.append({
            "cycle": self.current_cycle,
            "req_id": req_id,
            "path_id": path_id,
            "node": node,
            "success": success,
            "left_endpoints": list(left_endpoints),
            "right_endpoints": list(right_endpoints),
        })

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

    def handle_classic_packet(self, _node, msg):
        """FASE P1: Recepción de solicitudes."""
            
        if msg.get("cmd") == "QCAST_QUERY":
            self.pending_qcast_queries.append(msg)
            log.debug(f"QCastController: recibida petición {msg['req_id']} de {msg['src']}")

    def configure_cycle_routing(
        self,
        requests: list[dict[str, Any]],
        route_callback: Callable[[list[dict[str, str]]], None] | None = None,
    ) -> None:
        self.manage_routes_each_cycle = True
        self.active_cycle_requests = [
            {
                "req_id": str(req["req_id"]),
                "src": req["src"].name,
                "dst": req["dst"].name,
            }
            for req in requests
        ]
        self.cycle_route_callback = route_callback

    def _uninstall_cycle_paths(self) -> None:
        if self.net is None:
            raise RuntimeError("Q-CAST controller is not attached to a network")

        for path_id, route in list(self.path_route_names.items()):
            uninstall_msg = {"cmd": "UNINSTALL_PATH", "path_id": path_id}
            for node_name in route:
                node = self.net.get_node(node_name)
                forwarder = getattr(node, "forwarder", None)
                if forwarder is not None and path_id in forwarder.fib.table:
                    forwarder.handle_classic_packet(node, uninstall_msg)

        self.node_remaining_capacity.clear()
        self.edge_remaining_capacity.clear()
        self.request_route_info.clear()
        self.request_install_stats.clear()
        self.route_owner_req.clear()
        self.route_alias_reqs.clear()
        self.route_rr_index.clear()
        self.path_w.clear()
        self.path_channel_allocations.clear()
        self.path_requests.clear()
        self.main_paths_by_req.clear()
        self.path_route_names.clear()
        self.recovery_paths_info.clear()

    def handle(self, event):
        if isinstance(event, TimingPhaseEvent):
            self.handle_sync_phase(event)

    def handle_sync_phase(self, event: TimingPhaseEvent):
        phase_name = str(event.phase).split('.')[-1]

        if phase_name == "P1" and self.manage_routes_each_cycle:
            self._uninstall_cycle_paths()
            self.pending_qcast_queries = [dict(req) for req in self.active_cycle_requests]

        # Ejecutamos el enrutamiento en P2
        if phase_name == "P2":
            self.current_cycle += 1
            self._success_reported_this_cycle.clear()
            if self.pending_qcast_queries:
                self.priority_rng.shuffle(self.pending_qcast_queries)
                self.query_order_by_cycle[self.current_cycle] = [req.get("req_id") for req in self.pending_qcast_queries]
                inicio_calculo_rutas = time.perf_counter()
                if self.cycle_route_callback is None:
                    self._process_all_qcast_requests()
                else:
                    cycle_requests = list(self.pending_qcast_queries)
                    self.pending_qcast_queries.clear()
                    self.cycle_route_callback(cycle_requests)
                self.qcast_route_calc_time_total += time.perf_counter() - inicio_calculo_rutas
                self.qcast_route_calc_runs += 1
            self.request_route_history[self.current_cycle] = copy.deepcopy(self.request_route_info)
        
        # Ejecutamos la recuperación de rutas en P4
        elif phase_name == "P4":
            self.record_p4_phase()

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

        if bottleneck == float('inf') or len(route_objs) < 2 or self.net is None:
            return 0
        channel_width = min(
            self.edge_remaining_capacity.get(
                tuple(sorted((route_objs[i].name, route_objs[i + 1].name))),
                0,
            )
            for i in range(len(route_objs) - 1)
        )
        return min(int(bottleneck), channel_width)

    def _allocate_route_channels(
        self,
        route_objs: list[Any],
        path_id: int,
        width: int,
    ) -> dict[str, list[str]]:
        if self.net is None:
            raise RuntimeError("Q-CAST controller is not attached to a network")

        used_names = {
            channel_name
            for allocation in self.path_channel_allocations.values()
            for names in allocation.values()
            for channel_name in names
        }
        allocation: dict[str, list[str]] = {}
        for left, right in zip(route_objs[:-1], route_objs[1:]):
            edge_id = "|".join(sorted((left.name, right.name)))
            available = [
                channel.name
                for channel in self.net.get_qchannels_between(left.name, right.name)
                if channel.name not in used_names
            ]
            if len(available) < width:
                raise RuntimeError(
                    f"Insufficient physical channels for path {path_id} on edge {edge_id}: "
                    f"required={width}, available={len(available)}"
                )
            selected = available[:width]
            allocation[edge_id] = selected
            used_names.update(selected)

        self.path_channel_allocations[path_id] = allocation
        self.path_channel_allocation_history[path_id] = copy.deepcopy(allocation)
        return allocation

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
            return self.eda.query(
                src_node,
                dst_node,
                virtual_widths=virtual_widths,
                virtual_edge_widths=dict(self.edge_remaining_capacity),
            )
        finally:
            for left_node, right_node, p_link in removed_edges:
                self.eda.adj.setdefault(left_node, {})[right_node] = p_link

    def _initialize_node_capacity_once(self, nodes_list: list[Any]) -> None:
        """Initialize residual capacity once, then keep decrementing it as paths are installed."""
        if self.node_remaining_capacity:
            return
        if self.net is None:
            raise RuntimeError("Q-CAST controller is not attached to a network")

        self.node_remaining_capacity = {
            node: int(getattr(getattr(node, 'memory', None), 'capacity', 0))
            for node in nodes_list
        }
        self.edge_remaining_capacity = {
            tuple(sorted((left.name, right.name))): len(self.net.get_qchannels_between(left.name, right.name))
            for index, left in enumerate(nodes_list)
            for right in nodes_list[index + 1:]
            if self.net.get_qchannels_between(left.name, right.name)
        }

    def _process_all_qcast_requests(self):
        """
        FASE P2:
        1) Implementa G-EDA (Greedy Extended Dijkstra) fiel a Q-CAST.
        2) Calcula Rutas de Recuperación (Recovery Paths).
        """
        if not self.net:
            return

        nodes_list = list(getattr(self.net, 'all_nodes', list(getattr(self.net, 'nodes', []))))
        self._initialize_node_capacity_once(nodes_list)
        
        qchannels = getattr(self.net, 'qchannels', getattr(self.net, '_qchannels', []))
        self.eda.build(nodes_list, qchannels)

        remaining_queries = list(self.pending_qcast_queries)
        allocated_requests = [] 
        self.recovery_paths_info = {} 

        # ========================================================
        # LÓGICA CORE Q-CAST: UNA RUTA PRINCIPAL POR PETICIÓN
        # ========================================================
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
                result = self.eda.query(
                    src_node,
                    dst_node,
                    virtual_widths=dict(self.node_remaining_capacity),
                    virtual_edge_widths=dict(self.edge_remaining_capacity),
                )
                
                if result and len(result) > 0:
                    route_objs = result[0].route
                    metric = result[0].metric
                    
                    w_bottleneck = self._route_bottleneck_width(route_objs)
                    if self.max_alloc_width is not None and self.max_alloc_width > 0:
                        w_bottleneck = min(w_bottleneck, int(self.max_alloc_width))

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
                # Si no se encontró ninguna ruta válida para NINGUNA petición restante, la red está llena. Salimos del bucle.
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
            route_prob, route_fidelity = obtener_prob_y_fidelidad_de_ruta(
                self.net,
                route_objs,
                q_swap=self.q_swap,
            )
            route_hops = len(route_objs) - 1

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
            self.request_route_info[req_id]['w_asignado'] += w_real
            self.request_route_info[req_id]['multi_routes'].append({'route': route_names, 'w': w_real})
            self.request_success.setdefault(req_id, False)

            # Generación e instalación en FIB
            path_id = self.next_path_id
            self.next_path_id += 1
            self.path_w[path_id] = w_real
            self.path_route_names[path_id] = route_names
            self.path_route_history[path_id] = list(route_names)
            self.main_paths_by_req.setdefault(req_id, []).append(path_id)
            channels_by_edge = self._allocate_route_channels(route_objs, path_id, w_real)
            
            route_path = RoutingPathStatic(
                route_names,
                req_id=0,
                path_id=path_id,
                m_v=self._build_fair_m_v(route_names, w=w_real),
                swap=self.swap_policy,
            )
            instructions = next(route_path.compute_paths(self.net))
            cast(Any, instructions)["req_id"] = req_id
            cast(Any, instructions)["channels_by_edge"] = channels_by_edge
            install_msg = {"cmd": "INSTALL_PATH", "path_id": path_id, "instructions": instructions}
            
            for node_name in route_names:
                self._deliver_install_path(self.net.get_node(node_name), install_msg)
            
            self.path_requests[path_id] = [req_id]
            self.path_request_history[path_id] = [req_id]

            # Install one edge-disjoint end-to-end backup path. Segment-level
            # splicing is unsafe because each path has an immutable FIB context.
            self.recovery_paths_info[path_id] = []
            if not self.enable_recovery_paths:
                continue

            excluded_edges = list(zip(route_objs[:-1], route_objs[1:]))
            alt_result = self._query_route_without_edges(
                route_objs[0],
                route_objs[-1],
                excluded_edges,
                dict(self.node_remaining_capacity),
            )
            if alt_result:
                alt_route = alt_result[0].route
                alt_names = [node.name for node in alt_route]
                if alt_names != route_names:
                    self._install_recovery_candidate(
                        owner_req_id=req_id,
                        main_path_id=path_id,
                        candidate={
                            'segment_src': route_names[0],
                            'segment_dst': route_names[-1],
                            'route': alt_names,
                            'metric': alt_result[0].metric,
                            'width': self._route_bottleneck_width(alt_route),
                            'hops': len(alt_names) - 1,
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
                # ¡LA MAGIA! La guardamos en la sala de espera
                todavia_pendientes.append(req)
            
        # Actualizamos la lista oficial solo con los que NO consiguieron mesa
        self.pending_qcast_queries = todavia_pendientes

    def _consume_route_capacity(self, route_objs, w: int = 1) -> bool:
        if len(route_objs) < 2 or w <= 0: return False
        
        # 1. Chequeo
        for i, node in enumerate(route_objs):
            required = w if (i == 0 or i == len(route_objs) - 1) else (2 * w)
            if self.node_remaining_capacity.get(node, 0) < required:
                return False 
        edge_keys = [
            tuple(sorted((route_objs[i].name, route_objs[i + 1].name)))
            for i in range(len(route_objs) - 1)
        ]
        if any(self.edge_remaining_capacity.get(edge_key, 0) < w for edge_key in edge_keys):
            return False

        # 2. Consumo
        for i, node in enumerate(route_objs):
            consume = w if (i == 0 or i == len(route_objs) - 1) else (2 * w)
            self.node_remaining_capacity[node] -= consume
        for edge_key in edge_keys:
            self.edge_remaining_capacity[edge_key] -= w
            if self.edge_remaining_capacity[edge_key] == 0 and self.net is not None:
                left = self.net.get_node(edge_key[0])
                right = self.net.get_node(edge_key[1])
                self.eda.adj.get(left, {}).pop(right, None)
                self.eda.adj.get(right, {}).pop(left, None)
            
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
        self.path_route_history[rec_path_id] = list(alt_names)
        channels_by_edge = self._allocate_route_channels(alt_nodes, rec_path_id, rec_w)
        backup_req_id = f"{owner_req_id}__BACKUP_{rec_path_id}"
        rec_route_path = RoutingPathStatic(
            alt_names,
            req_id=rec_path_id,
            path_id=rec_path_id,
            m_v=self._build_fair_m_v(alt_names, w=rec_w),
            swap=self.swap_policy,
        )
        rec_instructions = next(rec_route_path.compute_paths(net))
        cast(Any, rec_instructions)["req_id"] = backup_req_id
        cast(Any, rec_instructions)["channels_by_edge"] = channels_by_edge
        rec_install_msg = {"cmd": "INSTALL_PATH", "path_id": rec_path_id, "instructions": rec_instructions}

        for node_name in alt_names:
            self._deliver_install_path(net.get_node(node_name), rec_install_msg)

        self.path_requests[rec_path_id] = [backup_req_id]
        self.path_request_history[rec_path_id] = [backup_req_id]
        self.recovery_paths_info[main_path_id].append({
            'segment_src': candidate.get('segment_src'),
            'segment_dst': candidate.get('segment_dst'),
            'route': alt_names,
            'metric': candidate.get('metric'),
            'hops': candidate.get('hops', max(0, len(alt_names) - 1)),
            'w': rec_w,
            'rec_path_id': rec_path_id,
        })
        self.recovery_path_history[rec_path_id] = {
            'cycle': self.current_cycle,
            'req_id': owner_req_id,
            'main_path_id': main_path_id,
            'main_route': list(self.path_route_names[main_path_id]),
            'route': alt_names,
            'metric': candidate.get('metric'),
            'hops': candidate.get('hops', max(0, len(alt_names) - 1)),
            'w': rec_w,
        }
        return True

    def report_success(
        self,
        req_id,
        time,
        fidelity: float | None = None,
        *,
        path_id: int | None = None,
        path_role: str = "main",
        epr_name: str | None = None,
    ):
        """Report an E2E delivery and track first satisfaction in its request-cycle."""
        _ = time
        if isinstance(req_id, str) and "__REC_" in req_id:
            log.debug(f"Ignoring non-E2E recovery-path completion: req_id={req_id}")
            return

        normalized_fidelity = float(fidelity) if fidelity is not None else None
        first_for_request_cycle = req_id not in self._success_reported_this_cycle
        counted = first_for_request_cycle or self.count_multiple_e2e_per_cycle
        if self.collect_validation_history:
            self.e2e_completion_history.append({
                "cycle": self.current_cycle,
                "req_id": req_id,
                "path_id": path_id,
                "path_role": path_role,
                "epr_name": epr_name,
                "fidelity": normalized_fidelity,
                "counted": counted,
                "first_for_request_cycle": first_for_request_cycle,
            })

        if not counted:
            return

        self._success_reported_this_cycle.add(req_id)
        self.successful_requests += 1
        self.request_success[req_id] = True
        self.request_success_count[req_id] = self.request_success_count.get(req_id, 0) + 1
        if normalized_fidelity is not None:
            self.request_fidelities.setdefault(req_id, []).append(normalized_fidelity)
        self.success_history.append({
            "cycle": self.current_cycle,
            "req_id": req_id,
            "fidelity": normalized_fidelity,
            "path_id": path_id,
            "path_role": path_role,
            "epr_name": epr_name,
        })

class QCastMultiEntController(QCastController):
    """Q-CAST variant that accepts every valid E2E delivery in a request-cycle."""

    count_multiple_e2e_per_cycle = True
