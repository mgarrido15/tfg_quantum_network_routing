import networkx as nx
from mqns.mqns.network.network.timing import TimingPhaseEvent
from mqns.mqns.network.fw.controller import RoutingController
from mqns.mqns.network.qcast.extended_dijkstra import QCastExtendedDijkstra

class QCastController(RoutingController):
    def __init__(self, k_max: int = 3):
        super().__init__()
        self.k_max = k_max
        self.pending_qcast_queries = []
        self.eda = QCastExtendedDijkstra()
        self.net = None
        

        self.successful_requests = 0
        self.completed_ids_in_cycle = set()
        self.request_route_info = {}
        self.request_success = {}

    def install(self, node):
        """Instala el controlador en un nodo físico"""
        self.node = node
        if hasattr(node, 'apps'):
            if self not in node.apps:
                node.apps.append(self)
        if hasattr(node, 'forwarder'):
            node.forwarder.controller = self

    def handle_classic_packet(self, node, msg):
        """
        FASE P1: Recepción de solicitudes.
        El controlador escucha los paquetes clásicos de los nodos.
        """
        if msg.get("cmd") == "QCAST_QUERY":
            self.pending_qcast_queries.append(msg)
            print(f"Controller: He recibido una solicitud de {msg['src']} hacia {msg['dst']}")

    def handle(self, event):
        """Puente para recibir los eventos de fase del simulador"""
        if isinstance(event, TimingPhaseEvent):
            self.handle_sync_phase(event)

    def handle_sync_phase(self, event: TimingPhaseEvent):
        """Manejador de las fases síncronas de Q-CAST"""
        phase_name = str(event.phase).split('.')[-1]
        
        if phase_name == "P1":
            self.completed_ids_in_cycle.clear()
            
        elif phase_name == "P2":
            if self.pending_qcast_queries:
                print(f"Controller: Procesando {len(self.pending_qcast_queries)} peticiones en fase P2...")
                self._process_all_qcast_requests()
                self.pending_qcast_queries.clear()

    def _process_all_qcast_requests(self):
        """
        Lógica central: Descubrimiento de topología + Dijkstra + Instalación de FIB
        """
        if not self.net:
            return

        nodes_list = self.net.all_nodes
        
        qchannels = getattr(self.net, 'qchannels', getattr(self.net, '_qchannels', []))
        
        self.eda.build(nodes_list, qchannels)
        
        for req in self.pending_qcast_queries:
            src_node = self.net.get_node(req["src"])
            dst_node = self.net.get_node(req["dst"])
            req_id = req["req_id"]

            virtual_widths = {n: getattr(n.memory, 'capacity', 10) for n in nodes_list}

            result = self.eda.query(src_node, dst_node, virtual_widths=virtual_widths)

            if result and len(result) > 0:
                route_objs = result[0].route
                route_names = [n.name for n in route_objs]
                route_prob = 1.0
                route_fidelity = 1.0
                for i in range(len(route_objs) - 1):
                    route_prob *= self.eda.adj[route_objs[i]][route_objs[i+1]]
                    qc = self._get_qchannel(route_objs[i], route_objs[i+1])
                    if qc:
                        init_fid = getattr(qc, '_fidelity', 0.99)
                        route_fidelity *= init_fid

                route_width = min(virtual_widths[n] for n in route_objs)
                route_hops = len(route_objs) - 1
                route_metric = result[0].metric

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

                print(f"RUTA ELEGIDA: {' -> '.join(route_names)}")
                print(f"  - Saltos: {route_hops}")
                print(f"  - Probabilidad de éxito estimada: {route_prob:.4f}")
                print(f"  - Fidelidad estimada: {route_fidelity:.4f}")
                print(f"  - Métrica EDA: {route_metric:.4f}")
                print(f"  - Ancho virtual de ruta: {route_width}")
                print("")

                for i in range(len(route_objs) - 1):
                    current_node = route_objs[i]
                    next_node = route_objs[i+1]
                    
                    fw = getattr(current_node, 'forwarder', None)
                    if fw:
                        if not hasattr(fw, 'fib') or fw.fib is None:
                            from types import SimpleNamespace
                            fw.fib = SimpleNamespace(table={})
                        
                        fw.fib.table[req_id] = next_node.name
                        print(f" FIB en {current_node.name}: Destino {dst_node.name} -> Salto {next_node.name}")
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
                print(f"Controller: No se encontró ruta para {req['src']} -> {req['dst']}")

    def _get_qchannel(self, node1, node2):
        """Obtener el canal cuántico entre dos nodos"""
        for qc in getattr(self.net, 'qchannels', []):
            if hasattr(qc, 'node_list'):
                if qc.node_list[0] == node1 and qc.node_list[1] == node2:
                    return qc
                if qc.node_list[0] == node2 and qc.node_list[1] == node1:
                    return qc
            else:
                if qc.node1 == node1 and qc.node2 == node2:
                    return qc
                if qc.node1 == node2 and qc.node2 == node1:
                    return qc
        return None

    def report_success(self, req_id, time):
        """
        MÉTRICA: Llamado por los nodos origen en Fase P4 cuando el entrelazamiento es E2E.
        """
        if req_id not in self.completed_ids_in_cycle:
            self.successful_requests += 1
            self.completed_ids_in_cycle.add(req_id)
            self.request_success[req_id] = True
        
        
