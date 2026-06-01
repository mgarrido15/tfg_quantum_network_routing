import networkx as nx
import os
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
    def __init__(self, k_max: int = 4):
        super().__init__()
        self.k_max = k_max
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
        self.request_install_stats = {}
        self.route_owner_req = {}
        self.route_alias_reqs = {}
        self.route_rr_index = {}
        self.path_requests = {}
        self.path_channel_allocations = {}
        self.request_route_info = {}
        self.pending_qcast_queries = []
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

    def handle_classic_packet(self, node, msg):
        """
        FASE P1: Recepción de solicitudes.
        """
        if msg.get("cmd") == "QCAST_QUERY":
            self.pending_qcast_queries.append(msg)

    def handle(self, event):
        if isinstance(event, TimingPhaseEvent):
            self.handle_sync_phase(event)

    def handle_sync_phase(self, event: TimingPhaseEvent):
        phase_name = str(event.phase).split('.')[-1]


        if phase_name == "P2":
            if self.pending_qcast_queries:
                self._process_all_qcast_requests()
                self.pending_qcast_queries.clear()

    def _deliver_install_path(self, qnode, install_msg):
        if qnode == self.node:
            fw = getattr(qnode, 'forwarder', None)
            if fw is not None and hasattr(fw, 'handle_classic_packet'):
                fw.handle_classic_packet(qnode, install_msg)
            return
        self.node.send_cpacket(qnode, ClassicPacket(install_msg, src=self.node, dest=qnode))

    def _build_fair_m_v(self, route_names: list[str], w: int = 1) -> list[tuple[int, int]]:
        """Construye el vector de multiplexación usando el ancho real W."""
        return [(w, w) for _ in range(max(0, len(route_names) - 1))]

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
        key = (req_id, path_id, qchannel, direction)
        self.path_channel_allocations[key] = {
            "requested": requested,
            "allocated": allocated,
            "success": success,
        }

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
        FASE P2:
        1) Implementa G-EDA (Greedy Extended Dijkstra) .
        2) Calcula Rutas de Recuperación (Recovery Paths) .
        """
        if not self.net:
            return

        # 1. Inicialización de la topología
        nodes_list = list(getattr(self.net, 'all_nodes', list(getattr(self.net, 'nodes', []))))
        
        # INICIALIZACIÓN CORRECTA: Leemos los cúbits REALMENTE LIBRES (RAW o ACTIVE)
        self.node_remaining_capacity = {}
        for node in nodes_list:
            if hasattr(node, "memory") and hasattr(node.memory, "qubits"):
                libres = sum(1 for q in node.memory.qubits if q.state.name in ["RAW", "ACTIVE"])
                self.node_remaining_capacity[node] = libres
            else:
                self.node_remaining_capacity[node] = getattr(getattr(node, 'memory', None), 'capacity', 0)
        
        qchannels = getattr(self.net, 'qchannels', getattr(self.net, '_qchannels', []))
        self.eda.build(nodes_list, qchannels)

        remaining_queries = list(self.pending_qcast_queries)
        allocated_requests = [] 

        if not hasattr(self, 'recovery_paths_info'):
            self.recovery_paths_info = {} 


        # SECCIÓN A: BUCLE GREEDY G-EDA 

        while remaining_queries:
            best_query = None
            best_result = None
            best_ext = -1.0

            # Evaluamos la mejor ruta para TODAS las solicitudes sobre el grafo residual actual
            for req in remaining_queries:
                src_node = self.net.get_node(req["src"])
                dst_node = self.net.get_node(req["dst"])
                
                result = self.eda.query(src_node, dst_node, virtual_widths=dict(self.node_remaining_capacity))
                if result and len(result) > 0:
                    metric = result[0].metric
                    if metric > best_ext:
                        best_ext = metric
                        best_result = result[0]
                        best_query = req

            if best_query and best_result:
                route_objs = best_result.route
                

                w_bottleneck = float('inf')
                for node in route_objs:
                    cap = self.node_remaining_capacity.get(node, 0)
                    if node != route_objs[0] and node != route_objs[-1]:
                        cap = cap // 2  
                    w_bottleneck = min(w_bottleneck, cap)
                
                w_bottleneck = max(1, int(w_bottleneck))

                if self._consume_route_capacity(best_result.route, w=w_bottleneck):
                    allocated_requests.append((best_query, best_result, w_bottleneck))
                    remaining_queries.remove(best_query)
                else:
                    log.warning(f"Reserva abortada para {best_query['req_id']}: sin capacidad física real.")
                    remaining_queries.remove(best_query) 
            else:
                break

        # SECCIÓN B: INSTALACIÓN DE RUTAS PRINCIPALES Y BÚSQUEDA DE RECUPERACIÓN

        for req, result, w_real in allocated_requests:
            req_id = req["req_id"]
            src_node = self.net.get_node(req["src"])
            dst_node = self.net.get_node(req["dst"])
            
            route_objs = result.route
            route_names = [n.name for n in route_objs]
            route_prob, route_fidelity = obtener_prob_y_fidelidad_de_ruta(self.net, route_objs)
            
            # Reajustamos el ancho visual basándonos en el estado
            route_width = min(self.node_remaining_capacity.get(node, 0) + 1 for node in route_objs)
            route_hops = len(route_objs) - 1

            route_key = tuple(route_names)
            self.route_owner_req[route_key] = req_id
            self.request_route_info[req_id] = {
                'src': src_node.name,
                'dst': dst_node.name,
                'route': route_names,
                'hops': route_hops,
                'metric': result.metric,
                'route_success_prob': route_prob,
                'route_fidelity': route_fidelity,
                'w_asignado': w_real,  
                'capacidad_residual_final': self._route_capacity_snapshot(route_objs),
            }
            self.request_success.setdefault(req_id, False)
            self.request_success_count.setdefault(req_id, 0)

            # Generación e instalación de la tabla de reenvío FIB
            path_id = self.next_path_id
            self.next_path_id += 1
            
            route_path = RoutingPathStatic(
                route_names,
                req_id=0,
                path_id=path_id,
                m_v=self._build_fair_m_v(route_names, w=w_real),
            )
            instructions = next(route_path.compute_paths(self.net))
            instructions["req_id"] = req_id
            install_msg = {"cmd": "INSTALL_PATH", "path_id": path_id, "instructions": instructions}
            for node_name in route_names:
                qnode = self.net.get_node(node_name)
                self._deliver_install_path(qnode, install_msg)
            
            self.path_requests[path_id] = [req_id]

            # CÁLCULO DE RUTAS DE RECUPERACIÓN (RECOVERY PATHS)
            self.recovery_paths_info[path_id] = []
            h = len(route_objs)
            
            for l in range(1, min(self.k_max, h)):
                for idx in range(h - l):
                    u = route_objs[idx]     
                    v = route_objs[idx + l] 
                    
                    alt_result = self.eda.query(u, v, virtual_widths=dict(self.node_remaining_capacity))
                    if alt_result and len(alt_result) > 0:
                        alt_route = alt_result[0].route
                        alt_names = [n.name for n in alt_route]
                        
                        segmento_original = [node.name for node in route_objs[idx:idx+l+1]]
                        if alt_names != segmento_original:
                            self.recovery_paths_info[path_id].append({
                                'segment_src': u.name,
                                'segment_dst': v.name,
                                'route': alt_names,
                                'metric': alt_result[0].metric
                            })
                            self._consume_route_capacity(alt_route)
                print(f"DEBUG: Rutas de recuperación encontradas para path_id={path_id}: {len(self.recovery_paths_info.get(path_id, []))}")

        # SECCIÓN C: MANEJO DE SOLICITUDES RECHAZADAS
        for req in remaining_queries:
            req_id = req["req_id"]
            self.request_route_info[req_id] = {
                'src': req["src"], 'dst': req["dst"], 'route': None, 'hops': 0, 'metric': 0.0,
                'route_success_prob': 0.0, 'route_fidelity': 0.0, 'width': 0
            }
            self.request_success.setdefault(req_id, False)
            self.request_success_count.setdefault(req_id, 0)
            
        self.pending_qcast_queries.clear()
        for path_id, recoveries in self.recovery_paths_info.items():
            if recoveries:
                print(f"\n[TEST Q-CAST] Ruta Principal {path_id} protegida con los siguientes desvíos:")
            for rec in recoveries:
                print(f" -> Si falla el tramo {rec['segment_src']}-{rec['segment_dst']}, usar ruta alternativa: {rec['route']}")

    def _route_capacity_snapshot(self, route_objs):
        return {node.name: self.node_remaining_capacity.get(node, 0) for node in route_objs}

    def _consume_route_capacity(self, route_objs, w: int = 1) -> bool:
        """
        Intenta consumir capacidad. Retorna True si tuvo éxito, False si no pudo.
        """
        if len(route_objs) < 2 or w <= 0:
            return False

        # 1. VERIFICACIÓN: ¿Podemos reservar 'w' en todos los nodos?
        for i, node in enumerate(route_objs):
            required = w if (i == 0 or i == len(route_objs) - 1) else (2 * w)
            if self.node_remaining_capacity.get(node, 0) < required:
                return False  # No se puede reservar esta ruta, abortamos

        # 2. EJECUCIÓN (Si pasamos el chequeo, es seguro restar)
        src_node, dst_node = route_objs[0], route_objs[-1]
        self.node_remaining_capacity[src_node] -= w
        self.node_remaining_capacity[dst_node] -= w
        
        for node in route_objs[1:-1]:
            self.node_remaining_capacity[node] -= (2 * w)
            
        return True

    def report_success(self, req_id, time, fidelity: float | None = None):

        # Simplificación: el éxito es para el req_id real
        target_req_id = req_id 

        try:
            self.successful_requests += 1
            self.request_success[target_req_id] = True
            self.request_success_count[target_req_id] = self.request_success_count.get(target_req_id, 0) + 1
            log.debug(f"Success counted for {target_req_id}")
        except Exception as e:
            log.error(f"Error counting success: {e}")

        if not hasattr(self, 'request_fidelities'):
            self.request_fidelities = {}

        if fidelity is None:
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
