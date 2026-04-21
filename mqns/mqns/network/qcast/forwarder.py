import random
from mqns.mqns.network.network.timing import TimingPhase, TimingPhaseEvent

class QCastForwarder:
    def __init__(self, k_max: int = 3):
        self.k_max = k_max
        self.request_sent = False 
        self.node = None
        self.fib = type('FIB', (), {'table': {}})() 
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
            for req_id in self.fib.table.items():
                success_prob = 0.9 
                if random.random() < success_prob:
                    self.entanglements[req_id] = True

        elif phase_name == "P4":
            for req_id in self.fib.table.items():
                if self.entanglements.get(req_id):
                    net = getattr(self.node, 'network', None)
                    if net and net.controller:
                        net.controller.report_success(req_id, event.t)

    def _send_initial_queries(self):
        net = getattr(self.node, 'network', getattr(self.node, 'net', None))
        if net and hasattr(net, 'controller') and net.controller:
            for req in net.requests:
                if req.src.name == self.node.name:
                    req_id = f"REQ_{req.src.name}_TO_{req.dst.name}"
                    msg = {"cmd": "QCAST_QUERY", "req_id": req_id, "src": self.node.name, "dst": req.dst.name}
                    net.controller.handle_classic_packet(self.node, msg)
                    self.request_sent = True