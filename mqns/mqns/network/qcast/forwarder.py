from typing import Any, cast
from mqns.network.fw.forwarder import Forwarder
from mqns.network.network.timing import TimingPhaseEvent
from mqns.entity.cchannel import ClassicPacket
from mqns.entity.memory import QubitState
from mqns.utils import log
from mqns.network.protocol.event import ManageActiveChannels
from mqns.network.protocol.event import QubitEntangledEvent
from mqns.network.fw.fib import FibEntry

class QCastForwarder(Forwarder):
    def __init__(self, k_max: int = 3, ps: float = 1.0, purif_enabled: bool = True, swapping_enabled: bool = True):
        super().__init__(ps=ps)
        self.k_max = k_max
        self.purif_enabled = purif_enabled
        self.swapping_enabled = swapping_enabled
        self.request_sent = False

    def install(self, node):
        super().install(node)
        self.add_handler(self.handle_qubit_entangled, QubitEntangledEvent)
        if hasattr(node, 'controller'):
            self.controller = node.controller

    def qubit_is_entangled(self, event):
        if hasattr(event, 'qubit') and hasattr(event.qubit, 'state'):
            if event.qubit.state.name != "ENTANGLED1":
                return 
        super().qubit_is_entangled(event)

    def qubit_is_purif(self, qubit, fib_entry, partner):
        if hasattr(self, 'controller') and self.controller and qubit.purif_rounds == 0:
            if hasattr(self.controller, 'record_eligible') and getattr(fib_entry, 'purif', None) is not None:
                # Record only when the qubit can transition to ELIGIBLE in this pass.
                segment_name = f"{self.node.name}-{partner.name}" if fib_entry.own_idx < fib_entry.find_index_and_swap_rank(partner.name)[0] else f"{partner.name}-{self.node.name}"
                if fib_entry.purif.get(segment_name, 0) == 0:
                    self.controller.record_eligible()
        qubit.trace_event("qcast_qubit_is_purif", self.simulator.tc, note=f"partner={partner.name}")
        super().qubit_is_purif(qubit, fib_entry, partner)

    def handle_qubit_entangled(self, event: QubitEntangledEvent):
        qubit = event.qubit
        path_id = getattr(qubit, 'path_id', None)
        if path_id is None:
            return

        entry = self.fib.get(path_id) if hasattr(self.fib, 'get') else None
        if not entry:
            return

        is_dest = False
        is_src = False
        if hasattr(entry, 'route') and isinstance(entry.route, list) and len(entry.route) > 0:
            is_dest = (entry.route[-1] == self.node.name)
            is_src = (entry.route[0] == self.node.name)
        elif hasattr(entry, 'dest'):
            destino = getattr(entry, 'dest', None)
            origen = getattr(entry, 'src', None)
            is_dest = (destino == self.node or getattr(destino, 'name', '') == self.node.name)
            is_src = (origen == self.node or getattr(origen, 'name', '') == self.node.name)

        # Caso A: Somos el destino (Consumimos el entrelazamiento final)
        if is_dest:
            qubit.trace_event("qcast_dest_entangled", self.simulator.tc, note=f"node={self.node.name}")
            self.consume_and_release(qubit)
            return

        # Caso C: Somos el origen (No hacemos swapping, solo esperamos)
        if is_src:
            qubit.trace_event("qcast_src_entangled_wait", self.simulator.tc, note=f"node={self.node.name}")
            return 

        # Caso B: Somos un nodo intermedio (intentar swapping)
        if getattr(self, 'swapping_enabled', True):
            qubit.trace_event("qcast_attempt_swapping", self.simulator.tc, note=f"node={self.node.name}")
            self.attempt_swapping(qubit)

    def do_swapping(self, mq0, mq1, fib_entry):
        try:
            super().do_swapping(mq0, mq1, fib_entry)
        except AssertionError:
            pass

    def attempt_swapping(self, qubit):
        path_id = getattr(qubit, 'path_id', None)
        if path_id is None:
            return
            
        def get_neighbor(ch):
            nodes = ch.node_list if hasattr(ch, 'node_list') else (ch.node1, ch.node2)
            return nodes[0] if nodes[1] == self.node else nodes[1]

        ch1 = getattr(qubit, 'qchannel', None)
        if not ch1: return
        vecino_actual = get_neighbor(ch1)

        def es_valido(q, _):
            if getattr(q, 'path_id', None) != path_id or q == qubit or not q.state.name.startswith("ENTANGLED"):
                return False
            ch2 = getattr(q, 'qchannel', None)
            if not ch2: return False
            otro_vecino = get_neighbor(ch2)
            return otro_vecino != vecino_actual

        matches = list(self.node.memory.find(es_valido))

        if matches:
            other_qubit, _ = matches[0]
            q1_name = getattr(qubit.qchannel, 'name', 'unknown')
            q2_name = getattr(other_qubit.qchannel, 'name', 'unknown')
            
            log.debug(f"{self.node}: Swapping path_id={path_id} entre hilos {q1_name} y {q2_name}")
            
            memory = cast(Any, self.node.memory)
            if hasattr(memory, 'perform_swapping'):
                memory.perform_swapping(qubit, other_qubit)
            elif hasattr(memory, 'ebsm'):
                memory.ebsm(qubit, other_qubit)

    def handle_path_change(self, *, path_id: int, uninstall: bool, fib_entry, l_neighbor, r_neighbor):
        from mqns.network.protocol.event import ManageActiveChannels
        w_asignado = 1
        if hasattr(self, 'controller') and self.controller:
            w_asignado = getattr(self.controller, 'path_w', {}).get(path_id, 1)

        def asegurar_recursos(canal):
            """
            Allocate a qubit to the given channel and path_id.
            If qubits already exist on the channel, assign path_id to the first one without one.
            Otherwise, take a RAW qubit from the memory pool and assign it.
            """
            conectados = list(self.node.memory.find(lambda *_: True, qchannel=canal))
            if len(conectados) > 0:
                # Preferred: Find a qubit on this channel that doesn't have a path_id yet
                for q, _ in conectados:
                    if getattr(q, 'path_id', None) is None:
                        q.path_id = path_id
                        log.debug(f"{self.node}: Asignado qubit {q.addr} existente en canal {canal.name} a path_id {path_id}")
                        return
                # Fallback: Log if all qubits on this channel are already allocated
                log.debug(f"{self.node}: Todos los qubits en {canal.name} ya tienen path_id, ninguno disponible para {path_id}")
            else:
                # No qubits on this channel yet, find a RAW one from the pool
                libres = [q for q in getattr(self.node.memory, 'qubits', []) 
                          if q.state.name == "RAW" and getattr(q, 'path_id', None) is None and getattr(q, 'qchannel', None) is None]
                if libres:
                    q = libres[0]
                    q.qchannel = canal
                    q.path_id = path_id
                    log.debug(f"{self.node}: Asignado qubit {q.addr} nuevo al canal {canal.name} con path_id {path_id}")
                else:
                    log.debug(f"{self.node}: No hay qubits RAW libres disponibles para {canal.name} con path_id {path_id}")

        for neighbor in [l_neighbor, r_neighbor]:
            if neighbor:
                vecino = neighbor[0]
                canales = self.node.network.get_qchannels_between(self.node.name, vecino.name)[:w_asignado]
                for qc in canales:
                    asegurar_recursos(qc)
                    if hasattr(self, 'controller') and self.controller and not uninstall and neighbor == r_neighbor:
                        if hasattr(self.controller, 'record_qchannel_activation'):
                            self.controller.record_qchannel_activation(path_id, qc.name)
                    if neighbor == r_neighbor: 
                        self.simulator.add_event(
                            ManageActiveChannels(
                                self.node, vecino, qc,
                                path_id=path_id, start=not uninstall, t=self.simulator.tc,
                            )
                        )
    
    def install_path_command(self, msg):
        path_id = msg.get("path_id")
        instructions = msg.get("instructions")
        
        route = instructions["route"]
        try:
            own_idx = route.index(self.node.name)
        except ValueError:
            return

        new_entry = FibEntry(
            path_id=path_id,
            req_id=instructions["req_id"],
            route=route,
            own_idx=own_idx,
            swap=instructions["swap"],
            swap_cutoff=instructions.get("swap_cutoff", []),
            purif=instructions.get("purif", {})
        )
        self.fib.insert_or_replace(new_entry)

        # --- CORRECCIÓN: BUSCAR CANAL Y CREAR TUPLA CORRECTA ---
        
        def get_neighbor_tuple(neighbor_name):
            if not neighbor_name: 
                return None
            
            neighbor_node = self.node.network.get_node(neighbor_name)
            # Obtenemos los canales disponibles entre los dos nodos
            channels = self.node.network.get_qchannels_between(self.node.name, neighbor_name)
            
            # Si hay canales, tomamos el primero disponible y devolvemos la tupla
            if channels:
                return (neighbor_node, channels[0])
            return None

        # Determinamos los nombres de los vecinos
        prev_name = route[own_idx - 1] if own_idx > 0 else None
        next_name = route[own_idx + 1] if own_idx < len(route) - 1 else None

        # Construimos las tuplas (QNode, QuantumChannel)
        l_neighbor = get_neighbor_tuple(prev_name)
        r_neighbor = get_neighbor_tuple(next_name)

        # Llamamos al gestor con los datos correctos
        self.handle_path_change(
            path_id=path_id,
            uninstall=False,
            fib_entry=new_entry,
            l_neighbor=l_neighbor,
            r_neighbor=r_neighbor
        )
        
        print(f"DEBUG: Nodo {self.node.name} activó protocolo para ruta {path_id}")

    def handle_classic_packet(self, node, msg):
        print(f"DEBUG: Nodo {self.node.name} recibió: {msg.get('cmd')}") 
        
        # Si el comando es para instalar una ruta
        if msg.get("cmd") == "INSTALL_PATH":
            self.install_path_command(msg)
            return

        local_event = type("_LocalClassicPacketEvent", (), {"packet": ClassicPacket(msg, src=node, dest=node)})()
        self.handle_classic_command(cast(Any, local_event))

    def consume_and_release(self, qubit):
        has_type = cast(Any, getattr(self, 'epr_type', None)) 
        _, qm = self.node.memory.read(qubit.addr, has=has_type, set_fidelity=True, remove=False)
        
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

            qubit.trace_event("qcast_consume", self.simulator.tc, note=f"node={self.node.name} fidelity={qm.fidelity}")
                
        # 1. Liberamos el qubit local (Destino)
        self.release_qubit(qubit)

        # 2. FIX CRÍTICO: Liberamos el qubit remoto (Origen) para evitar el Memory Leak!
        remote_qubit = getattr(qubit, 'entangled_qubit', None)
        if remote_qubit is not None:
            if hasattr(remote_qubit.node, 'forwarder'):
                remote_qubit.node.forwarder.release_qubit(remote_qubit)
            else:
                remote_qubit.state = QubitState.RAW
                remote_qubit.path_id = None
                remote_qubit.trace_event("qcast_remote_release_raw", self.simulator.tc, note=f"node={remote_qubit.node.name}")

    def handle_sync_phase(self, event: TimingPhaseEvent):
        phase_name = str(event.phase).split('.')[-1]
        
        # FIX DE Q-CAST: En la fase P1 (Inicio de ciclo), limpiamos la basura del ciclo anterior
        if phase_name == "P1":
            self._aggressive_cleanup()
            if not self.request_sent:
                self._send_initial_queries()
                self.request_sent = True
                
        super().handle_sync_phase(event)

    def _aggressive_cleanup(self):
        """
        Clean up stuck qubits at the start of each cycle (P1).
        In Q-CAST with multipath, qubits can get stuck if one path finishes 
        before another. This cleanup recycles them for new paths.
        
        States that indicate a qubit has FINISHED its lifecycle and can be recycled:
        - PURIF or PENDING for extended duration (3+ rounds)
        - ELIGIBLE without progress
        - ENTANGLED* states that weren't consumed or swapped
        
        States to PRESERVE (normal processing):
        - RAW, ACTIVE (just started), RESERVED (in progress)
        """
        current_tc = self.simulator.tc
        current_sec = current_tc.sec
        
        for q in getattr(self.node.memory, 'qubits', []):
            q_state = q.state.name if hasattr(q.state, 'name') else str(q.state)
            has_path_id = getattr(q, 'path_id', None) is not None
            purif_rounds = getattr(q, 'purif_rounds', 0)
            creation_time = getattr(q, 'creation_time', current_tc)
            time_in_state = (current_tc - creation_time).sec if hasattr(creation_time, 'accuracy') else current_sec
            
            # Clean qubits that are clearly finished/stuck
            if has_path_id:
                # If PURIF/PENDING for extended time, it's stuck
                if q_state in ["PURIF", "PENDING"] and (purif_rounds > 3 or time_in_state > 10):
                    log.debug(f"{self.node}: CLEANUP: Qubit {q.addr} stuck in {q_state} for {time_in_state}, recycling")
                    q.trace_event("qcast_cleanup_recycle", self.simulator.tc, note=f"from={q_state}")
                    q.reset_state()
                    q.path_id = None
                    q.qchannel = None
                # If ELIGIBLE but hasn't been consumed, it might be stuck
                elif q_state == "ELIGIBLE" and time_in_state > 5 and purif_rounds >= 2:
                    log.debug(f"{self.node}: CLEANUP: Qubit {q.addr} ELIGIBLE but not consumed, recycling")
                    q.trace_event("qcast_cleanup_recycle", self.simulator.tc, note="from=ELIGIBLE")
                    q.reset_state()
                    q.path_id = None
                    q.qchannel = None

    def _send_initial_queries(self):
        net = getattr(self.node, 'network', None)
        if net and hasattr(net, 'controller') and net.controller:
            for req in net.requests:
                if req.src == self.node:
                    req_id = req.attr.get("req_id", f"REQ_{req.src.name}_TO_{req.dst.name}")
                    msg = {"cmd": "QCAST_QUERY", "req_id": req_id, "src": req.src.name, "dst": req.dst.name}
                    net.controller.handle_classic_packet(self.node, msg)