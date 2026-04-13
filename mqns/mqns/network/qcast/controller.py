import sys
import os
from typing import override



try:
    from mqns.mqns.network.fw.controller import RoutingController
except ImportError:

    from mqns.mqns.network.network import RoutingController

from mqns.mqns.network.network.timing import TimingPhase, TimingPhaseEvent
from mqns.mqns.utils import log


from .extended_dijkstra import QCastExtendedDijkstra

class QCastController(RoutingController):
    def __init__(self, k_max: int = 3):
        super().__init__()
        self.pending_qcast_queries = []
        self.successful_requests = 0
        self.completed_ids = set() 
        self.eda = QCastExtendedDijkstra()


    def install(self, node):
        """Asocia el controlador al nodo físico"""
        self.node = node
        self.net = getattr(node, 'network', getattr(node, 'net', None))
        
        if self.net:
            nodes_list = list(self.net.nodes.values()) if isinstance(self.net.nodes, dict) else self.net.nodes
            qchannels = getattr(self.net, 'qchannels', getattr(self.net, '_qchannels', []))
            self.eda.build(nodes_list, qchannels)
            print(f" QCastController: Grafo construido en {node.name}")

    def handle(self, event):
        """Método puente para el timing.py"""
        if isinstance(event, TimingPhaseEvent):
            self.handle_sync_phase(event)

    def handle_classic_packet(self, src, msg):
        """Recibe mensajes de control clásicos"""
        if isinstance(msg, dict) and msg.get("cmd") == "QCAST_QUERY":
            # Confirmación visual inmediata
            print(f" Controller: He recibido una solicitud de {msg['src']} hacia {msg['dst']}")
            
            if not any(q['req_id'] == msg['req_id'] for q in self.pending_qcast_queries):
                self.pending_qcast_queries.append(msg)

    def handle_sync_phase(self, event: TimingPhaseEvent):
        phase_name = str(event.phase).split('.')[-1]

        if phase_name == "P1":
            self.completed_ids.clear()

        if phase_name == "P2":
            if self.pending_qcast_queries:
                print(f"  Controller: Procesando {len(self.pending_qcast_queries)} peticiones en fase {phase_name}...")
                self._process_all_qcast_requests()
                self.pending_qcast_queries.clear()

    def _process_all_qcast_requests(self):
        if not self.net:
            print(" Controller: No hay referencia a la red (self.net)")
            return

        for req in self.pending_qcast_queries:
            src_node = self.net.get_node(req["src"])
            dst_node = self.net.get_node(req["dst"])
            
            nodes_list = self.net.all_nodes
            virtual_widths = {n: getattr(n.memory, 'capacity', 10) for n in nodes_list}
            
            result = self.eda.query(src_node, dst_node, virtual_widths=virtual_widths)
            
  
            if result and len(result) > 0:
                best_path_result = result[0]                              
                route_objs = best_path_result.route
                route_names = [n.name for n in route_objs]
                print(f"  RUTA ELEGIDA: {' -> '.join(route_names)}")
                

                req_id = req["req_id"]
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
                print(f" No se encontró ruta para la petición de {req['src']}")
    
    def report_success(self, req_id, time):
        if req_id not in self.completed_ids:
            self.successful_requests += 1
            self.completed_ids.add(req_id)
            print(f" [t={time}] ÉXITO E2E: {req_id} completada.")