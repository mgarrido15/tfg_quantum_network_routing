import random
from dataclasses import dataclass, field

from mqns.network.network.timing import TimingPhase, TimingPhaseEvent


@dataclass
class FIB:
    table: dict[str, str] = field(default_factory=dict)

class QCastForwarder:
    def __init__(self, k_max: int = 3):
        self.k_max = k_max
        self.request_sent = False 
        self.node = None
        self.fib = FIB()
        self.entanglements = {} 

    def install(self, node):
        self.node = node

    def handle(self, event):
        if isinstance(event, TimingPhaseEvent):
            self.handle_sync_phase(event)

    def handle_sync_phase(self, event: TimingPhaseEvent):
        phase_name = str(event.phase).split('.')[-1]
        
        if phase_name == "P1":
            if not self.request_sent:
                self._send_initial_queries()

        elif phase_name == "P3":
            self.entanglements.clear() 
            for req_id in self.fib.table.keys():
                net = getattr(self.node, 'network', None)
                success_prob = 0.0
                if net and getattr(net, 'controller', None):
                    route_info = getattr(net.controller, 'request_route_info', {})
                    info = route_info.get(req_id, None)
                    if info is not None:
                        success_prob = float(info.get('route_success_prob', 0.0))
                if random.random() < success_prob:
                    self.entanglements[req_id] = True

        elif phase_name == "P4":
            for req_id in self.fib.table.keys():
                if self.entanglements.get(req_id):
                    net = getattr(self.node, 'network', None)
                    if net and net.controller:
                        net.controller.report_success(req_id, event.t)

    def _send_initial_queries(self):
        net = getattr(self.node, 'network', getattr(self.node, 'net', None))
        if net and hasattr(net, 'controller') and net.controller:
            for req in net.requests:
                src_node = getattr(req, 'src', None)
                dst_node = getattr(req, 'dst', None)
                if src_node is None or dst_node is None or self.node is None:
                    continue

                src_name = getattr(src_node, 'name', None)
                dst_name = getattr(dst_node, 'name', None)
                node_name = getattr(self.node, 'name', None)
                if src_name is None or dst_name is None or node_name is None:
                    continue

                if src_name == node_name:
                    req_id = req.attr.get("req_id", f"REQ_{src_name}_TO_{dst_name}")
                    msg = {"cmd": "QCAST_QUERY", "req_id": req_id, "src": node_name, "dst": dst_name}
                    net.controller.handle_classic_packet(self.node, msg)
                    self.request_sent = True