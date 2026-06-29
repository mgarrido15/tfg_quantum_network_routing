import networkx as nx
import os
from typing import Any, cast
from mqns.entity.cchannel import ClassicPacket
from mqns.network.network.timing import TimingPhaseEvent
from mqns.network.network.reporting import obtener_prob_y_fidelidad_de_ruta
from mqns.network.fw.controller import RoutingController
from mqns.network.fw.routing import RoutingPathStatic
from mqns.network.qcast.extended_dijkstra import QCastExtendedDijkstra
from mqns.utils import log

class QCastController(RoutingController):
    def __init__(self, k_max: int = 4):
        super().__init__()
        self.k_max = k_max
        self.paths = []
        self.pending_qcast_queries = []
        self.eda = QCastExtendedDijkstra()
        self.net = None
        
        # Diccionarios de estado interno
        self.node_remaining_capacity = {}
        self.successful_requests = 0
        self.request_route_info = {}
        self.request_success = {}
        self.request_success_count = {}
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
            print(f"DEBUG Controller: Recibida petición {msg['req_id']} de {msg['src']}")

    def handle(self, event):
        if isinstance(event, TimingPhaseEvent):
            self.handle_sync_phase(event)

    def handle_sync_phase(self, event: TimingPhaseEvent):
        phase_name = str(event.phase).split('.')[-1]
        
        # Ejecutamos el enrutamiento en P2
        if phase_name == "P2":
            if self.pending_qcast_queries:
                self._process_all_qcast_requests()
                # BORRA O COMENTA ESTA LÍNEA PARA QUE NO DESTRUYA LA COLA:
                # self.pending_qcast_queries.clear() 
        
        # Ejecutamos la recuperación de rutas en P4
        elif phase_name == "P4":
            self.record_p4_phase()
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

    def _process_all_qcast_requests(self):
        """
        FASE P2:
        1) Implementa G-EDA (Greedy Extended Dijkstra) fiel a Q-CAST.
        2) Calcula Rutas de Recuperación (Recovery Paths).
        """
        if not self.net:
            return

        nodes_list = list(getattr(self.net, 'all_nodes', list(getattr(self.net, 'nodes', []))))
        
        # INICIALIZACIÓN: Leer capacidad física real
        self.node_remaining_capacity = {}
        for node in nodes_list:
            if hasattr(node, "memory") and hasattr(node.memory, "qubits"):
                # Q-CAST original cuenta qubits que no estén ya entrelazados o reservados permanentemente
                libres = sum(1 for q in node.memory.qubits if q.state.name in ["RAW", "ACTIVE"])
                self.node_remaining_capacity[node] = libres
            else:
                self.node_remaining_capacity[node] = getattr(getattr(node, 'memory', None), 'capacity', 0)
        
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
                result = self.eda.query(src_node, dst_node, virtual_widths=dict(self.node_remaining_capacity))
                
                if result and len(result) > 0:
                    route_objs = result[0].route
                    metric = result[0].metric
                    
                    # Calcular el cuello de botella (bottleneck) de esta ruta
                    w_bottleneck = float('inf')
                    for i, node in enumerate(route_objs):
                        cap = self.node_remaining_capacity.get(node, 0)
                        if i != 0 and i != len(route_objs) - 1:
                            cap = cap // 2  # Nodos intermedios necesitan 2 qubits por swap
                        w_bottleneck = min(w_bottleneck, cap)
                    
                    w_bottleneck = int(w_bottleneck)

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

        for req, result, w_real in allocated_requests:
            req_id = req["req_id"]
            allocated_req_ids.add(req_id)
            
            src_node = self.net.get_node(req["src"])
            dst_node = self.net.get_node(req["dst"])
            route_objs = result.route
            route_names = [n.name for n in route_objs]
            route_prob, route_fidelity = obtener_prob_y_fidelidad_de_ruta(self.net, route_objs)
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
            
            # --- 1. CREACIÓN DEL GRAFO RESIDUAL ---
            # Hacemos una copia de la memoria disponible justo después de instalar la ruta principal
            memoria_residual = dict(self.node_remaining_capacity)
            
            h = len(route_objs)
            for l in range(1, min(self.k_max, h)):
                for idx in range(h - l):
                    u = route_objs[idx]     
                    v = route_objs[idx + l] 
                    
                    alt_result = self.eda.query(u, v, virtual_widths=memoria_residual)
                    if not alt_result:
                        alt_result = self.eda.query(u, v)
                    
                    if alt_result and len(alt_result) > 0:
                        alt_route = alt_result[0].route
                        alt_names = [n.name for n in alt_route]
                        segmento_original = [node.name for node in route_objs[idx:idx+l+1]]
                        
                        if alt_names != segmento_original:
                            if not self._consume_route_capacity(alt_route, w=1):
                                log.debug(
                                    f"Q-CAST recovery path accepted without residual-capacity reservation: "
                                    f"req_id={req_id} segment={u.name}-{v.name} route={alt_names}"
                                )

                            rec_path_id = self.next_path_id
                            self.next_path_id += 1
                            self.path_w[rec_path_id] = 1
                            self.path_route_names[rec_path_id] = alt_names
                            
                            rec_route_path = RoutingPathStatic(alt_names, req_id=req_id, path_id=rec_path_id, m_v=self._build_fair_m_v(alt_names, w=1))
                            rec_instructions = next(rec_route_path.compute_paths(self.net))
                            rec_instructions["req_id"] = req_id
                            rec_install_msg = {"cmd": "INSTALL_PATH", "path_id": rec_path_id, "instructions": rec_instructions}
                            
                            for node_name in alt_names:
                                self._deliver_install_path(self.net.get_node(node_name), rec_install_msg)

                            self.recovery_paths_info[path_id].append({
                                'segment_src': u.name, 'segment_dst': v.name,
                                'route': alt_names, 'metric': alt_result[0].metric, 'rec_path_id': rec_path_id
                            })

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
                        if not self._consume_route_capacity(alt_route, w=1):
                            log.debug(
                                f"Q-CAST fallback recovery accepted without residual-capacity reservation: "
                                f"req_id={req_id} route={alt_names}"
                            )

                        rec_path_id = self.next_path_id
                        self.next_path_id += 1
                        self.path_w[rec_path_id] = 1
                        self.path_route_names[rec_path_id] = alt_names

                        rec_route_path = RoutingPathStatic(alt_names, req_id=req_id, path_id=rec_path_id, m_v=self._build_fair_m_v(alt_names, w=1))
                        rec_instructions = next(rec_route_path.compute_paths(self.net))
                        rec_instructions["req_id"] = req_id
                        rec_install_msg = {"cmd": "INSTALL_PATH", "path_id": rec_path_id, "instructions": rec_instructions}

                        for node_name in alt_names:
                            self._deliver_install_path(self.net.get_node(node_name), rec_install_msg)

                        self.recovery_paths_info[path_id].append({
                            'segment_src': route_objs[0].name,
                            'segment_dst': route_objs[-1].name,
                            'route': alt_names,
                            'metric': alt_result[0].metric,
                            'rec_path_id': rec_path_id,
                        })

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

        # 2. Consumo
        for i, node in enumerate(route_objs):
            consume = w if (i == 0 or i == len(route_objs) - 1) else (2 * w)
            self.node_remaining_capacity[node] -= consume
            
        return True

    def report_success(self, req_id, time, fidelity: float | None = None):
        try:
            self.successful_requests += 1
            self.request_success[req_id] = True
            self.request_success_count[req_id] = self.request_success_count.get(req_id, 0) + 1
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

                    best_patch = None
                    for rec in recoveries:
                        rec_path_id = rec['rec_path_id']
                        patch_ready = all(
                            self._check_segment_entangled(rec['route'][j], rec['route'][j + 1], rec_path_id)
                            for j in range(len(rec['route']) - 1)
                        )
                        if patch_ready and self._patch_covers_segment(br_u, br_v, rec):
                            best_patch = rec
                            break

                    if best_patch is None:
                        for rec in recoveries:
                            rec_path_id = rec['rec_path_id']
                            patch_ready = all(
                                self._check_segment_entangled(rec['route'][j], rec['route'][j + 1], rec_path_id)
                                for j in range(len(rec['route']) - 1)
                            )
                            if patch_ready:
                                best_patch = rec
                                break

                    if best_patch:
                        repaired_segments.add((br_u, br_v))
                        self.record_p4_recovery_applied()
                        log.info(f"Q-CAST P4 REPARADO: Fallo en {br_u}-{br_v}. Usando desvío: {best_patch['route']}")
                        self._apply_patch_swapping(path_id, best_patch)

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


class QCastFidelityController(QCastController):
    """Q-CAST variant that prioritizes route fidelity in its routing decisions.
    
    Uses QCastExtendedDijkstraFidelity which includes fidelity in the metric:
    metric = width * probability * fidelity
    """
    def __init__(self, k_max: int = 3):
        super().__init__(k_max=k_max)
        self.eda = QCastExtendedDijkstra()
