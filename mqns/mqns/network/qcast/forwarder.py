import random
from typing import Any, cast
from mqns.network.fw.forwarder import Forwarder
from mqns.network.network.timing import TimingPhaseEvent
from mqns.entity.cchannel import ClassicPacket
from mqns.utils import log

class QCastForwarder(Forwarder):
    def __init__(self, k_max: int = 3, ps: float = 1.0, purif_enabled: bool = True, swapping_enabled: bool = True):
        super().__init__(ps=ps)
        self.k_max = k_max
        self.purif_enabled = purif_enabled
        self.swapping_enabled = swapping_enabled
        self.request_sent = False

    def install(self, node):
        super().install(node)
        if hasattr(node, 'controller'):
            self.controller = node.controller

    def handle_path_change(self, *, path_id: int, uninstall: bool, fib_entry, l_neighbor, r_neighbor):
        from mqns.network.protocol.event import ManageActiveChannels
        if r_neighbor:
            self.simulator.add_event(
                ManageActiveChannels(
                    self.node, r_neighbor[0], r_neighbor[1],
                    path_id=path_id, start=not uninstall, t=self.simulator.tc,
                )
            )

    def handle_classic_packet(self, node, msg):
        local_event = type(
            "_LocalClassicPacketEvent",
            (),
            {"packet": ClassicPacket(msg, src=node, dest=node)},
        )()
        self.handle_classic_command(cast(Any, local_event))

    def consume_and_release(self, qubit):
        _, qm = self.memory.read(qubit.addr, has=self.epr_type, set_fidelity=True, remove=True)
        net = getattr(self.node, 'network', None)
        controller = getattr(net, 'controller', None)
        path_id = getattr(qubit, 'path_id', None)
        
        if controller is not None and path_id is not None:
            entry = self.fib.get(path_id) if hasattr(self.fib, 'get') else None
            req_id = getattr(entry, 'req_id', None)
            if req_id:
                try:
                    log.debug(f"{self.node}: QCAST_REPORT_SUCCESS req_id={req_id} path_id={path_id} fidelity={qm.fidelity}")
                except Exception:
                    pass
                controller.report_success(req_id, self.simulator.tc, fidelity=qm.fidelity)
        self.release_qubit(qubit)

    def handle_sync_phase(self, event: TimingPhaseEvent):
        phase_name = str(event.phase).split('.')[-1]
        if phase_name == "P1" and not self.request_sent:
            self._send_initial_queries()
            self.request_sent = True
        super().handle_sync_phase(event)

    def _send_initial_queries(self):
        net = getattr(self.node, 'network', None)
        if net and hasattr(net, 'controller') and net.controller:
            for req in net.requests:
                if req.src == self.node:
                    req_id = req.attr.get("req_id", f"REQ_{req.src.name}_TO_{req.dst.name}")
                    msg = {"cmd": "QCAST_QUERY", "req_id": req_id, "src": req.src.name, "dst": req.dst.name}
                    net.controller.handle_classic_packet(self.node, msg)