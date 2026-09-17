from typing import Any, cast
from mqns.network.fw.forwarder import Forwarder
from mqns.network.network.timing import TimingPhaseEvent
from mqns.entity.cchannel import ClassicPacket
from mqns.entity.memory import PathDirection, QubitState
from mqns.utils import log
from mqns.network.protocol.event import ManageActiveChannels
from mqns.network.fw.fib import FibEntry
from mqns.network.fw.classic import fw_control_cmd_handler

class QCastForwarder(Forwarder):
    def __init__(self, k_max: int = 3, ps: float = 1.0, purif_enabled: bool = True, swapping_enabled: bool = True):
        super().__init__(ps=ps)
        self.k_max = k_max
        self.purif_enabled = purif_enabled
        self.swapping_enabled = swapping_enabled
        self.request_sent = False
        self._edge_path_channels: dict[tuple[str, str], dict[int, list[str]]] = {}
        self._consumed_pending_eprs: set[str] = set()

    def _edge_key(self, a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def _select_channels_for_edge(
        self,
        *,
        neighbor_name: str,
        path_id: int,
        width: int,
        uninstall: bool,
        selected_names: list[str] | None = None,
    ) -> list[Any]:
        """Prefer channels not used by other active paths on the same physical edge."""
        all_channels = self.node.network.get_qchannels_between(self.node.name, neighbor_name)
        if not all_channels or width <= 0:
            return []

        edge_key = self._edge_key(self.node.name, neighbor_name)
        edge_alloc = self._edge_path_channels.setdefault(edge_key, {})

        if uninstall:
            selected_names = edge_alloc.pop(path_id, [])
            if selected_names:
                selected_set = set(selected_names)
                return [qc for qc in all_channels if qc.name in selected_set]
            return all_channels[:width]

        if selected_names is not None:
            selected_set = set(selected_names)
            selected = [qc for qc in all_channels if qc.name in selected_set]
            if len(selected) != width:
                raise RuntimeError(
                    f"{self.node}: controller selected {len(selected_names)} channels for "
                    f"path {path_id} on {edge_key}, but {len(selected)} are available locally"
                )
            edge_alloc[path_id] = [qc.name for qc in selected]
            return selected

        used_by_others: set[str] = set()
        for other_path_id, names in edge_alloc.items():
            if other_path_id != path_id:
                used_by_others.update(names)

        preferred = [qc for qc in all_channels if qc.name not in used_by_others]
        selected = preferred[:width]

        if len(selected) < width:
            fallback = [qc for qc in all_channels if qc.name in used_by_others and qc.name not in selected]
            selected.extend(fallback[: width - len(selected)])

        edge_alloc[path_id] = [qc.name for qc in selected]
        return selected

    def install(self, node):
        super().install(node)
        if hasattr(node, 'controller'):
            self.controller = node.controller

    def qubit_is_entangled(self, event):
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

    def can_consume(self, fib_entry, epr):
        if fib_entry is None:
            return super().can_consume(fib_entry, epr)
        return fib_entry.own_idx == len(fib_entry.route) - 1

    def qubit_is_eligible(self, qubit, fib_entry):
        if fib_entry is not None and fib_entry.own_idx == 0:
            return
        super().qubit_is_eligible(qubit, fib_entry)

    def do_swapping(self, mq0, mq1, fib_entry):
        _, epr0 = self.node.memory.read(mq0.addr, has=self.epr_type)
        _, epr1 = self.node.memory.read(mq1.addr, has=self.epr_type)
        swaps_before = self.cnt.n_swapped
        super().do_swapping(mq0, mq1, fib_entry)
        if (
            hasattr(self, "controller")
            and self.controller
            and self.controller.collect_validation_history
        ):
            self.controller.record_swap(
                req_id=fib_entry.req_id,
                path_id=fib_entry.path_id,
                node=self.node.name,
                success=self.cnt.n_swapped > swaps_before,
                left_endpoints=(
                    getattr(getattr(epr0, "src", None), "name", None),
                    getattr(getattr(epr0, "dst", None), "name", None),
                ),
                right_endpoints=(
                    getattr(getattr(epr1, "src", None), "name", None),
                    getattr(getattr(epr1, "dst", None), "name", None),
                ),
            )

    def attempt_swapping(self, qubit):
        path_id = getattr(qubit, 'path_id', None)
        if path_id is None:
            return

        # Resolve all sibling path_ids of the same request so that any
        # successfully entangled channel on one link can be paired with any
        # successfully entangled channel on the adjacent link.
        try:
            entry = self.fib.get(path_id) if hasattr(self.fib, 'get') else None
            req_id = entry.req_id if entry else None
        except (IndexError, AttributeError):
            req_id = None

        if req_id is not None and hasattr(self.fib, 'list_path_ids_by_request_id'):
            valid_path_ids = set(self.fib.list_path_ids_by_request_id(req_id))
        else:
            valid_path_ids = {path_id}

        def get_neighbor(ch):
            nodes = ch.node_list if hasattr(ch, 'node_list') else (ch.node1, ch.node2)
            return nodes[0] if nodes[1] == self.node else nodes[1]

        ch1 = getattr(qubit, 'qchannel', None)
        if not ch1:
            return
        vecino_actual = get_neighbor(ch1)

        def es_valido(q, _):
            if getattr(q, 'path_id', None) not in valid_path_ids or q == qubit or not q.state.name.startswith("ENTANGLED"):
                return False
            ch2 = getattr(q, 'qchannel', None)
            if not ch2:
                return False
            otro_vecino = get_neighbor(ch2)
            return otro_vecino != vecino_actual

        matches = list(self.node.memory.find(es_valido))

        if matches:
            other_qubit, _ = matches[0]
            q1_name = getattr(qubit.qchannel, 'name', 'unknown')
            q2_name = getattr(other_qubit.qchannel, 'name', 'unknown')

            log.debug(f"{self.node}: Swapping req_id={req_id} path_ids={path_id}/{other_qubit.path_id} entre hilos {q1_name} y {q2_name}")

            # Normalise path_id so downstream nodes track the entanglement consistently.
            other_qubit.path_id = path_id

            memory = cast(Any, self.node.memory)
            if hasattr(memory, 'perform_swapping'):
                memory.perform_swapping(qubit, other_qubit)
            elif hasattr(memory, 'ebsm'):
                memory.ebsm(qubit, other_qubit)

    def handle_path_change(
        self,
        *,
        path_id: int,
        uninstall: bool,
        fib_entry,
        l_neighbor,
        r_neighbor,
        channels_by_edge: dict[str, list[str]] | None = None,
    ):
        from mqns.network.protocol.event import ManageActiveChannels
        w_asignado = 1
        if hasattr(self, 'controller') and self.controller:
            w_asignado = getattr(self.controller, 'path_w', {}).get(path_id, 1)

        def asegurar_recursos(canal, direction):
            available = next(
                self.node.memory.find(
                    lambda q, _: q.state == QubitState.RAW and q.path_id is None,
                    qchannel=canal,
                ),
                None,
            )
            if available is None:
                reusable = next(
                    self.node.memory.find(
                        lambda q, _: (
                            q.state == QubitState.RAW
                            and q.active is None
                            and q.path_id is None
                        )
                    ),
                    None,
                )
                if reusable is None:
                    raise OverflowError(
                        f"{self.node}: no RAW qubit available for path {path_id} "
                        f"on channel {canal.name}"
                    )

                reusable_qubit, _ = reusable
                self.node.memory.unassign(reusable_qubit.addr)
                self.node.memory.assign(canal, n=1)

            addrs = self.node.memory.allocate(
                canal,
                path_id,
                direction,
                n=1,
            )
            log.debug(
                f"{self.node}: allocated qubit {addrs[0]} on {canal.name} "
                f"for path {path_id}"
            )

        for neighbor in [l_neighbor, r_neighbor]:
            if neighbor:
                vecino = neighbor[0]
                canales = self._select_channels_for_edge(
                    neighbor_name=vecino.name,
                    path_id=path_id,
                    width=w_asignado,
                    uninstall=uninstall,
                    selected_names=(
                        channels_by_edge.get("|".join(self._edge_key(self.node.name, vecino.name)))
                        if channels_by_edge is not None
                        else None
                    ),
                )
                for qc in canales:
                    if not uninstall:
                        direction = PathDirection.L if neighbor == l_neighbor else PathDirection.R
                        asegurar_recursos(qc, direction)
                    else:
                        addrs = [
                            q.addr
                            for q, _ in self.node.memory.find(
                                lambda q, _: q.path_id == path_id,
                                qchannel=qc,
                            )
                        ]
                        self.node.memory.deallocate(*addrs)
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
            swap_cutoff=[
                None if cutoff < 0 else self.simulator.time(time_slot=cutoff)
                for cutoff in instructions.get("swap_cutoff", [])
            ],
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
            r_neighbor=r_neighbor,
            channels_by_edge=instructions.get("channels_by_edge"),
        )

        log.debug(f"{self.node}: activó protocolo para ruta {path_id}")

    @fw_control_cmd_handler("INSTALL_PATH")
    def handle_install_path(self, msg):
        self.install_path_command(msg)

    def handle_classic_packet(self, node, msg):
        log.debug(f"{self.node}: recibió mensaje clásico {msg.get('cmd')}")
        
        # Si el comando es para instalar una ruta
        if msg.get("cmd") == "INSTALL_PATH":
            self.install_path_command(msg)
            return

        local_event = type("_LocalClassicPacketEvent", (), {"packet": ClassicPacket(msg, src=node, dest=node)})()
        self.handle_classic_command(cast(Any, local_event))

    def _handle_swap_update(self, msg, fib_entry):
        new_epr_name = msg["new_epr"]
        if new_epr_name in self._consumed_pending_eprs:
            self._consumed_pending_eprs.remove(new_epr_name)
            old_pair = self.memory.read(msg["epr"])
            if old_pair is not None:
                self.release_qubit(old_pair[0], need_remove=True)
            return
        super()._handle_swap_update(msg, fib_entry)

    def _release_epr_endpoints(self, local_qubit, epr) -> bool:
        if epr.src == self.node:
            remote_node = epr.dst
        elif epr.dst == self.node:
            remote_node = epr.src
        else:
            raise RuntimeError(
                f"{self.node}: cannot release EPR {epr.name}; "
                "the consuming node is not one of its endpoints"
            )

        if remote_node is None:
            raise RuntimeError(f"{self.node}: EPR {epr.name} has no remote endpoint")

        remote_matches = list(remote_node.memory.find(lambda _qubit, stored: stored is epr))
        remote_forwarder = remote_node.get_app(Forwarder)
        if not remote_matches:
            pending_epr = remote_forwarder.remote_swapped_eprs.get(epr.name)
            if pending_epr is epr and isinstance(remote_forwarder, QCastForwarder):
                remote_forwarder.remote_swapped_eprs.pop(epr.name)
                remote_forwarder._consumed_pending_eprs.add(epr.name)
                self.release_qubit(local_qubit, need_remove=True)
                return True
            controller = getattr(getattr(self.node, "network", None), "controller", None)
            if controller is not None:
                controller.orphaned_e2e_count += 1
            log.warning(
                f"{self.node}: discarding orphaned EPR {epr.name}; "
                f"remote endpoint {remote_node} no longer stores it"
            )
            self.release_qubit(local_qubit, need_remove=True)
            return False
        if len(remote_matches) > 1:
            raise RuntimeError(
                f"{self.node}: expected one remote copy of EPR {epr.name} at "
                f"{remote_node}, found {len(remote_matches)}; "
                f"tc={self.simulator.tc}, path_id={getattr(local_qubit, 'path_id', None)}, "
                f"local_state={getattr(local_qubit, 'state', None)}"
            )
        remote_qubit, _ = remote_matches[0]
        remote_forwarder.release_qubit(remote_qubit, need_remove=True)
        self.release_qubit(local_qubit, need_remove=True)
        return True

    def _record_e2e_candidate(self, epr, req_id, path_id) -> None:
        if not self.controller.collect_validation_history:
            return
        elementary_eprs = epr.orig_eprs or [epr]
        self.controller.record_e2e_candidate(
            req_id=req_id,
            path_id=path_id,
            epr_name=epr.name,
            endpoints=(
                getattr(getattr(epr, "src", None), "name", None),
                getattr(getattr(epr, "dst", None), "name", None),
            ),
            elementary_eprs=[
                {
                    "name": elementary.name,
                    "src": getattr(getattr(elementary, "src", None), "name", None),
                    "dst": getattr(getattr(elementary, "dst", None), "name", None),
                    "channel_index": elementary.ch_index,
                }
                for elementary in elementary_eprs
            ],
        )

    def consume_and_release(self, qubit):
        has_type = cast(Any, getattr(self, 'epr_type', None)) 
        _, qm = self.node.memory.read(qubit.addr, has=has_type, set_fidelity=True, remove=False)
        
        net = getattr(self.node, 'network', None)
        controller = getattr(net, 'controller', None)
        path_id = getattr(qubit, 'path_id', None)
        
        if controller is not None and path_id is not None:
            entry = self.fib.get(path_id) if hasattr(self.fib, 'get') else None
            req_id = getattr(entry, 'req_id', None)
            if req_id and "__REC_" not in req_id:
                report_req_id = req_id.split("__BACKUP_", 1)[0]
                route = getattr(entry, "route", [])
                expected_endpoints = {route[0], route[-1]} if len(route) >= 2 else set()
                actual_endpoints = {
                    getattr(getattr(qm, "src", None), "name", None),
                    getattr(getattr(qm, "dst", None), "name", None),
                }
                if actual_endpoints != expected_endpoints:
                    controller.invalid_e2e_endpoint_count = (
                        getattr(controller, "invalid_e2e_endpoint_count", 0) + 1
                    )
                    log.error(
                        f"{self.node}: refusing non-E2E completion req_id={report_req_id} "
                        f"expected={sorted(expected_endpoints)} actual={sorted(str(v) for v in actual_endpoints)}"
                    )
                    self._release_epr_endpoints(qubit, qm)
                    return
                if not self._release_epr_endpoints(qubit, qm):
                    return
                self.cnt.increment_n_consumed(qm.fidelity)
                self._record_e2e_candidate(qm, report_req_id, path_id)
                log.debug(
                    f"{self.node}: QCAST_REPORT_SUCCESS req_id={report_req_id} "
                    f"path_id={path_id} fidelity={qm.fidelity}"
                )
                controller.report_success(
                    report_req_id,
                    self.simulator.tc,
                    fidelity=qm.fidelity,
                    path_id=path_id,
                    path_role="backup" if "__BACKUP_" in str(req_id) else "main",
                    epr_name=qm.name,
                )
            elif not self._release_epr_endpoints(qubit, qm):
                return
            else:
                self.cnt.increment_n_consumed(qm.fidelity)

            qubit.trace_event("qcast_consume", self.simulator.tc, note=f"node={self.node.name} fidelity={qm.fidelity}")
            return

        if self._release_epr_endpoints(qubit, qm):
            self.cnt.increment_n_consumed(qm.fidelity)

    def handle_sync_phase(self, event: TimingPhaseEvent):
        phase_name = str(event.phase).split('.')[-1]
        
        # FIX DE Q-CAST: En la fase P1 (Inicio de ciclo), limpiamos la basura del ciclo anterior
        if phase_name == "P1":
            self._aggressive_cleanup()
            manages_cycles = bool(
                getattr(getattr(self, "controller", None), "manage_routes_each_cycle", False)
            )
            if not self.request_sent and not manages_cycles:
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
        self._consumed_pending_eprs.clear()
        
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
