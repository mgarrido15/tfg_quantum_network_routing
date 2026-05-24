import networkx as nx
from typing import Any, cast
from mqns.entity.cchannel import ClassicPacket
from mqns.network.network.timing import TimingPhaseEvent
from mqns.network.network.reporting import obtener_prob_y_fidelidad_de_ruta, estimar_fidelidad_observada_de_ruta
from mqns.network.fw.controller import RoutingController
from mqns.network.fw.routing import RoutingPathStatic
from mqns.network.route.route import RouteQueryResult
from mqns.network.qcast.extended_dijkstra import QCastExtendedDijkstra, QCastExtendedDijkstraFidelity
from mqns.utils import log

class QCastController(RoutingController):
    def __init__(self, k_max: int = 3):
        super().__init__()
        self.k_max = k_max
        # Optional path list used by scripts/controllers that pre-install routes.
        self.paths = []
        self.pending_qcast_queries = []
        self.eda = QCastExtendedDijkstra()
        self.net = None
        self.node_remaining_capacity = {}
        

        self.successful_requests = 0
        self.completed_ids_in_cycle = set()
        self.request_route_info = {}
        self.request_success = {}
        self.request_success_count = {}
        # Per-request installation diagnostics (channel allocations and failures)
        self.request_install_stats = {}
        # Route deduplication state: one installed path can serve repeated requests.
        self.route_owner_req = {}
        self.route_alias_reqs = {}
        self.route_rr_index = {}
        # NEW: Track which requests use each path (path_id -> [req_ids])
        self.path_requests = {}
        # NEW: Track channel allocations per request
        self.path_channel_allocations = {}
        self.request_route_info = {}
        self.pending_qcast_queries = []
        # IMPORTANTE: Definir la lista de rutas para que Pylance no falle
        self.path_requests: dict[int, list[str]] = {}

    def install(self, node):
        """Instala el controlador en un nodo físico"""
        self.node = node
        if hasattr(node, 'apps') and self not in node.apps:
            node.apps.append(self)
        if hasattr(node, 'forwarder'):
            node.forwarder.controller = self
        self.net = self.node.network
        self.next_req_id = getattr(self, 'next_req_id', 0)
        self.next_path_id = getattr(self, 'next_path_id', 0)

    def _select_best_fidelity_route(self, src_node, dst_node, virtual_widths: dict | None = None):
        """Select the best route by explicit path enumeration for fidelity-aware Q-CAST.

        This keeps the routing rule aligned with the requested metric:
        Q-CAST score multiplied by route fidelity.
        """
        if not self.net:
            return None

        virtual_widths = virtual_widths or {}
        channels = getattr(self.net, 'qchannels', getattr(self.net, '_qchannels', []))

        graph = nx.Graph()
        for qc in channels:
            if hasattr(qc, 'node_list'):
                u, v = qc.node_list[0], qc.node_list[1]
            else:
                u, v = getattr(qc, 'node1', None), getattr(qc, 'node2', None)
            if u is None or v is None:
                continue
            graph.add_edge(u, v)

        if src_node not in graph or dst_node not in graph:
            return None

        best = None
        best_score = -1.0
        best_fidelity = -1.0

        try:
            paths = nx.all_simple_paths(graph, src_node, dst_node, cutoff=max(1, len(graph.nodes) - 1))
        except Exception:
            return None

        for route_objs in paths:
            if len(route_objs) < 2:
                continue
            if any(virtual_widths.get(node, getattr(getattr(node, 'memory', None), 'capacity', 1)) <= 0 for node in route_objs):
                continue

            route_prob, route_fidelity = obtener_prob_y_fidelidad_de_ruta(self.net, route_objs)
            estimated_observed_fidelity = estimar_fidelidad_observada_de_ruta(self.net, route_objs)
            route_width = min(virtual_widths.get(node, getattr(getattr(node, 'memory', None), 'capacity', 1)) for node in route_objs)
            score = route_width * route_prob * estimated_observed_fidelity

            if score > best_score or (score == best_score and route_fidelity > best_fidelity):
                best_score = score
                best_fidelity = route_fidelity
                best = {
                    'route': route_objs,
                    'route_names': [node.name for node in route_objs],
                    'route_prob': route_prob,
                    'route_fidelity': estimated_observed_fidelity,
                    'route_width': route_width,
                    'route_metric': score,
                    'hops': len(route_objs) - 1,
                }

        return best

    def handle_classic_packet(self, node, msg):
        """
        FASE P1: Recepción de solicitudes.
        El controlador escucha los paquetes clásicos de los nodos.
        """
        if msg.get("cmd") == "QCAST_QUERY":
            self.pending_qcast_queries.append(msg)

    def handle(self, event):
        """Puente para recibir los eventos de fase del simulador"""
        if isinstance(event, TimingPhaseEvent):
            self.handle_sync_phase(event)

    def handle_sync_phase(self, event: TimingPhaseEvent):
        """Manejador de las fases síncronas de Q-CAST"""
        phase_name = str(event.phase).split('.')[-1]

        if phase_name == "P1":
            self.completed_ids_in_cycle.clear()
            self._send_initial_queries()

        elif phase_name == "P2":
            if self.pending_qcast_queries:
                self._process_all_qcast_requests()
                self.pending_qcast_queries.clear()

    def _deliver_install_path(self, qnode, install_msg):
        """Deliver INSTALL_PATH locally on controller or via classic channel otherwise."""
        if qnode == self.node:
            fw = getattr(qnode, 'forwarder', None)
            if fw is not None and hasattr(fw, 'handle_classic_packet'):
                fw.handle_classic_packet(qnode, install_msg)
            return
        self.node.send_cpacket(qnode, ClassicPacket(install_msg, src=self.node, dest=qnode))

    def _send_initial_queries(self):
        """Enviar queries desde el nodo del controlador para solicitudes que originen aquí"""
        if not self.net:
            return

        requests = getattr(self.net, 'requests', [])

        for req in requests:
            src_node = getattr(req, 'src', None)

            if src_node is None or src_node != self.node:
                continue

            dst_node = getattr(req, 'dst', None)
            if dst_node is None:
                continue

            req_id = req.attr.get("req_id", f"REQ_{src_node.name}_TO_{dst_node.name}")
            msg = {"cmd": "QCAST_QUERY", "req_id": req_id, "src": src_node.name, "dst": dst_node.name}
            self.handle_classic_packet(self.node, msg)

    def _build_fair_m_v(self, route_names: list[str]) -> list[tuple[int, int]]:
        """Build a conservative multiplexing vector: reserve one qubit per side and hop."""
        return [(1, 1) for _ in range(max(0, len(route_names) - 1))]

    def record_channel_allocation(
        self,
        req_id,
        *,
        path_id: int,
        qchannel: str,
        direction: str,
        requested: int,
        allocated: int,
        success: bool,
    ) -> None:
        stats = self.request_install_stats.setdefault(
            req_id,
            {
                "ok": 0,
                "fail": 0,
                "events": [],
            },
        )
        if success:
            stats["ok"] += 1
        else:
            pass
            stats["fail"] += 1
        stats["events"].append(
            {
                "path_id": path_id,
                "qchannel": qchannel,
                "direction": direction,
                "requested": requested,
                "allocated": allocated,
                "success": success,
            }
        )
        # NEW: Track allocations per request per channel
        key = (req_id, path_id, qchannel, direction)
        self.path_channel_allocations[key] = {
            "requested": requested,
            "allocated": allocated,
            "success": success,
        }

    def register_route_alias(self, owner_req_id, alias_req_id) -> None:
        if owner_req_id == alias_req_id:
            return
        aliases = self.route_alias_reqs.setdefault(owner_req_id, [])
        if alias_req_id not in aliases:
            aliases.append(alias_req_id)

    def _pick_success_target(self, owner_req_id):
        pool = [owner_req_id] + self.route_alias_reqs.get(owner_req_id, [])
        if len(pool) == 0:
            return owner_req_id
        idx = self.route_rr_index.get(owner_req_id, 0)
        target = pool[idx % len(pool)]
        self.route_rr_index[owner_req_id] = idx + 1
        return target

    def _process_all_qcast_requests(self):
        """
        Lógica central: Descubrimiento de topología + Dijkstra + Instalación de FIB
        
        CORRECCIÓN: Permite que múltiples solicitudes del MISMO par origen-destino
        usen paths paralelos si hay capacidad, en lugar de "ahogarlas" todas en
        un único path alias.
        """
        if not self.net:
            return

        nodes_list = list(getattr(self.net, 'all_nodes', list(getattr(self.net, 'nodes', []))))
        if not self.node_remaining_capacity:
            self.node_remaining_capacity = {
                node: getattr(getattr(node, 'memory', None), 'capacity', 1)
                for node in nodes_list
            }
        
        qchannels = getattr(self.net, 'qchannels', getattr(self.net, '_qchannels', []))

        self.eda.build(nodes_list, qchannels)

        for req in self.pending_qcast_queries:
            # Recompute virtual widths from the live remaining capacity so each
            # request in the same P2 phase sees updated capacities after prior
            # allocations. This prevents multiple requests from reusing the
            # same node capacity when it should have been consumed.
            virtual_widths = dict(self.node_remaining_capacity)

            src_node = self.net.get_node(req["src"])
            dst_node = self.net.get_node(req["dst"])
            req_id = req["req_id"]

            # If route is already assigned (from pre-routing), skip recomputation
            if req_id in self.request_route_info:
                # Route was already assigned externally, just ensure FIB is installed
                existing_route = self.request_route_info[req_id].get('route')
                if existing_route:
                    route_objs = [self.net.get_node(n) for n in existing_route if self.net.get_node(n)]
                    route_fits = all(self.node_remaining_capacity.get(node, 0) > 0 for node in route_objs)

                    if not route_fits:
                        # If the originally assigned route no longer fits, do NOT
                        # attempt to find an alternative for plain Dijkstra
                        # variants (classic / distance). Those must fail instead
                        # of being rerouted.
                        route_algo = getattr(self.net, 'route', None)
                        algo_name = getattr(route_algo, '__class__', None) and route_algo.__class__.__name__ or None
                        if algo_name in ('DijkstraRouteAlgorithm', 'DijkstraDistanceRouteAlgorithm'):
                            # Fail the request: leave route as None
                            self.request_route_info[req_id] = {
                                'src': src_node.name,
                                'dst': dst_node.name,
                                'route': None,
                                'hops': 0,
                                'metric': 0.0,
                                'route_success_prob': 0.0,
                                'route_fidelity': 0.0,
                                'width': 0,
                            }
                            self.request_success.setdefault(req_id, False)
                            self.request_success_count.setdefault(req_id, 0)
                            continue

                        result = self.eda.query(src_node, dst_node, virtual_widths=dict(self.node_remaining_capacity))
                        if result and len(result) > 0:
                            route_objs = result[0].route
                            existing_route = [node.name for node in route_objs]
                            route_fits = all(self.node_remaining_capacity.get(node, 0) > 0 for node in route_objs)

                    if not route_objs or not route_fits:
                        self.request_route_info[req_id] = {
                            'src': src_node.name,
                            'dst': dst_node.name,
                            'route': None,
                            'hops': 0,
                            'metric': 0.0,
                            'route_success_prob': 0.0,
                            'route_fidelity': 0.0,
                            'width': 0,
                        }
                        self.request_success.setdefault(req_id, False)
                        self.request_success_count.setdefault(req_id, 0)
                        continue

                    route_names = list(existing_route)
                    route_key = tuple(route_names)
                    route_prob, route_fidelity = obtener_prob_y_fidelidad_de_ruta(self.net, route_objs)
                    observed_route_fidelity = (
                        estimar_fidelidad_observada_de_ruta(self.net, route_objs)
                        if isinstance(self.eda, QCastExtendedDijkstraFidelity)
                        else route_fidelity
                    )
                    route_width = min(self.node_remaining_capacity.get(node, 0) for node in route_objs)
                    route_hops = len(route_objs) - 1
                    # Use fidelity-weighted metric for fidelity-aware controller
                    if isinstance(self.eda, QCastExtendedDijkstraFidelity):
                        route_metric = route_width * route_prob * observed_route_fidelity
                    else:
                        route_metric = route_hops

                    self.request_route_info[req_id] = {
                        'src': src_node.name,
                        'dst': dst_node.name,
                        'route': route_names,
                        'hops': route_hops,
                        'metric': route_metric,
                        'route_success_prob': route_prob,
                        'route_fidelity': observed_route_fidelity,
                        'width': route_width,
                    }
                    self.request_success.setdefault(req_id, False)
                    self.request_success_count.setdefault(req_id, 0)

                    self._consume_route_capacity(route_objs)

                    path_id = self.next_path_id
                    self.next_path_id += 1
                    route_path = RoutingPathStatic(
                        route_names,
                        req_id=0,
                        path_id=path_id,
                        m_v=self._build_fair_m_v(route_names),
                    )
                    instructions = next(route_path.compute_paths(self.net))
                    instructions["req_id"] = req_id
                    install_msg = {"cmd": "INSTALL_PATH", "path_id": path_id, "instructions": instructions}
                    for node_name in route_names:
                        qnode = self.net.get_node(node_name)
                        self._deliver_install_path(qnode, install_msg)
                    self.path_requests.setdefault(path_id, []).append(req_id)
                    continue

            result = self.eda.query(src_node, dst_node, virtual_widths=virtual_widths)

            if isinstance(self.eda, QCastExtendedDijkstraFidelity):
                best_route = self._select_best_fidelity_route(src_node, dst_node, virtual_widths=virtual_widths)
                if best_route is not None:
                    route_objs = best_route['route']
                    result = [
                        RouteQueryResult(
                            metric=best_route['route_metric'],
                            next_hop=route_objs[1],
                            route=route_objs,
                        )
                    ]

            if result and len(result) > 0:
                route_objs = result[0].route
                route_names = [n.name for n in route_objs]
                route_prob, route_fidelity = obtener_prob_y_fidelidad_de_ruta(self.net, route_objs)
                observed_route_fidelity = (
                    estimar_fidelidad_observada_de_ruta(self.net, route_objs)
                    if isinstance(self.eda, QCastExtendedDijkstraFidelity)
                    else route_fidelity
                )

                route_width = min(virtual_widths[n] for n in route_objs)
                route_hops = len(route_objs) - 1
                # Prefer fidelity-weighted metric for fidelity-aware controller
                if isinstance(self.eda, QCastExtendedDijkstraFidelity):
                    route_metric = route_width * route_prob * observed_route_fidelity
                else:
                    route_metric = result[0].metric

                route_key = tuple(route_names)
                owner_req_id = self.route_owner_req.get(route_key)
                
                if owner_req_id is not None:
                    # Cada request consume capacidad de forma independiente.
                    # Si la ruta aún cabe, se instala otra instancia; si no, se
                    # intenta una ruta alternativa con la capacidad restante.
                    if not all(self.node_remaining_capacity.get(n, 0) > 0 for n in route_objs):
                        # For plain Dijkstra (classic / distance) do not try an
                        # alternative: the communication must fail if the
                        # originally computed route no longer has capacity.
                        route_algo = getattr(self.net, 'route', None)
                        algo_name = getattr(route_algo, '__class__', None) and route_algo.__class__.__name__ or None
                        if algo_name in ('DijkstraRouteAlgorithm', 'DijkstraDistanceRouteAlgorithm'):
                            self.request_route_info[req_id] = {
                                'src': src_node.name,
                                'dst': dst_node.name,
                                'route': None,
                                'hops': 0,
                                'metric': 0.0,
                                'route_success_prob': 0.0,
                                'route_fidelity': 0.0,
                                'width': 0,
                            }
                            self.request_success.setdefault(req_id, False)
                            self.request_success_count.setdefault(req_id, 0)
                            continue

                        alt = self.eda.query(src_node, dst_node, virtual_widths=dict(self.node_remaining_capacity))
                        if alt and len(alt) > 0:
                            route_objs = alt[0].route
                            route_names = [n.name for n in route_objs]
                            route_prob, route_fidelity = obtener_prob_y_fidelidad_de_ruta(self.net, route_objs)
                            observed_route_fidelity = (
                                estimar_fidelidad_observada_de_ruta(self.net, route_objs)
                                if isinstance(self.eda, QCastExtendedDijkstraFidelity)
                                else route_fidelity
                            )
                            route_width = min(self.node_remaining_capacity.get(n, 0) for n in route_objs)
                            route_hops = len(route_objs) - 1
                            if isinstance(self.eda, QCastExtendedDijkstraFidelity):
                                route_metric = route_width * route_prob * observed_route_fidelity
                            else:
                                route_metric = alt[0].metric
                            route_key = tuple(route_names)
                        else:
                            self.request_route_info[req_id] = {
                                'src': src_node.name,
                                'dst': dst_node.name,
                                'route': None,
                                'hops': 0,
                                'metric': 0.0,
                                'route_success_prob': 0.0,
                                'route_fidelity': 0.0,
                                'width': 0,
                            }
                            self.request_success.setdefault(req_id, False)
                            self.request_success_count.setdefault(req_id, 0)
                            continue

                    self.route_owner_req[route_key] = req_id

                # Nueva ruta: registrar como owner
                self.route_owner_req[route_key] = req_id
                self.request_route_info[req_id] = {
                    'src': src_node.name,
                    'dst': dst_node.name,
                    'route': route_names,
                    'hops': route_hops,
                    'metric': route_metric,
                    'route_success_prob': route_prob,
                    'route_fidelity': route_fidelity,
                    'width': route_width,
                }
                self.request_success.setdefault(req_id, False)
                self.request_success_count.setdefault(req_id, 0)

                self._consume_route_capacity(route_objs)

                path_id = self.next_path_id
                self.next_path_id += 1
                route_path = RoutingPathStatic(
                    route_names,
                    req_id=0,
                    path_id=path_id,
                    m_v=self._build_fair_m_v(route_names),
                )
                instructions = next(route_path.compute_paths(self.net))
                instructions["req_id"] = req_id
                install_msg = {"cmd": "INSTALL_PATH", "path_id": path_id, "instructions": instructions}
                for node_name in route_names:
                    qnode = self.net.get_node(node_name)
                    self._deliver_install_path(qnode, install_msg)
                
                # Track which requests use this path
                self.path_requests[path_id] = [req_id]
            else:
                self.request_route_info[req_id] = {
                    'src': src_node.name,
                    'dst': dst_node.name,
                    'route': None,
                    'hops': 0,
                    'metric': 0.0,
                    'route_success_prob': 0.0,
                    'route_fidelity': 0.0,
                    'width': 0,
                }
                self.request_success.setdefault(req_id, False)
                self.request_success_count.setdefault(req_id, 0)

    def _route_capacity_snapshot(self, route_objs):
        return {node.name: self.node_remaining_capacity.get(node, 0) for node in route_objs}

    def _consume_route_capacity(self, route_objs):
        for node in route_objs:
            current = self.node_remaining_capacity.get(node, 0)
            self.node_remaining_capacity[node] = max(0, current - 1)

    def _get_qchannel(self, node1, node2):
        """Obtener el canal cuántico entre dos nodos"""
        for qc in getattr(self.net, 'qchannels', []):
            if hasattr(qc, 'node_list'):
                if qc.node_list[0] == node1 and qc.node_list[1] == node2:
                    return qc
                if qc.node_list[0] == node2 and qc.node_list[1] == node1:
                    return qc
            else:
                if getattr(qc, 'node1', None) == node1 and getattr(qc, 'node2', None) == node2:
                    return qc
                if getattr(qc, 'node1', None) == node2 and getattr(qc, 'node2', None) == node1:
                    return qc
        return None

    def report_success(self, req_id, time, fidelity: float | None = None):
        """
        MÉTRICA: Llamado por los nodos origen en Fase P4 cuando el entrelazamiento es E2E.

        Ahora acepta un `fidelity` opcional. Si se proporciona, se registra como
        muestra observada para la solicitud `req_id`. Si no, intenta usar la
        fidelidad de ruta precomputada.
        """
        try:
            log.debug(f"Controller.report_success received req_id={req_id} time={time} fidelity={fidelity}")
        except Exception:
            pass
        # Also write a simple append-only trace for reliable diagnostics
        try:
            with open("outputs/report_success.log", "a", encoding="utf-8") as _f:
                _f.write(f"RECV req_id={req_id} time={time} fidelity={fidelity}\n")
        except Exception:
            pass

        target_req_id = self._pick_success_target(req_id)
        try:
            log.debug(f"Controller.report_success mapped owner {req_id} -> target {target_req_id}")
        except Exception:
            pass

        # Count every received success (more reliable for plain Dijkstra flows)
        try:
            self.successful_requests += 1
            self.request_success[target_req_id] = True
            self.request_success_count[target_req_id] = self.request_success_count.get(target_req_id, 0) + 1
            counted = True
            try:
                log.debug(f"Controller.report_success counted success for {target_req_id}; total={self.request_success_count[target_req_id]}")
            except Exception:
                pass
        except Exception:
            counted = False

        try:
            with open("outputs/report_success.log", "a", encoding="utf-8") as _f:
                _f.write(f"MAP owner={req_id} target={target_req_id} counted={counted}\n")
        except Exception:
            pass

        # Registrar fidelidad observada por request
        if not hasattr(self, 'request_fidelities'):
            self.request_fidelities = {}

        if fidelity is None:
            # Intentar obtener fidelidad de la info de ruta, fallback a 0.0
            route_info = getattr(self, 'request_route_info', {})
            info = route_info.get(target_req_id, None)
            fidelity = info.get('route_fidelity') if info is not None else 0.0

        fidelity_value = float(fidelity) if fidelity is not None else 0.0
        self.request_fidelities.setdefault(target_req_id, []).append(fidelity_value)


class QCastFidelityController(QCastController):
    """Q-CAST variant that prioritizes route fidelity in its routing decisions.
    
    Uses QCastExtendedDijkstraFidelity which includes fidelity in the metric:
    metric = width * probability * fidelity
    """
    def __init__(self, k_max: int = 3):
        super().__init__(k_max=k_max)
        self.eda = QCastExtendedDijkstraFidelity()
