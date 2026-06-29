from abc import ABC, abstractmethod
from collections import deque
from enum import Enum, auto
from typing import TYPE_CHECKING, final, override

from mqns.simulator import Event, Time, func_to_event
from mqns.utils import log

if TYPE_CHECKING:
    from mqns.network.network import QuantumNetwork


class TimingPhase(Enum):
    P1 = auto()
    P2 = auto()
    P3 = auto()
    P4 = auto()
    ROUTING = P2
    EXTERNAL = P3
    INTERNAL = P4


@final
class TimingPhaseEvent(Event):
    """
    Event that indicates a timing phase change, emitted in SYNC timing mode only.
    """

    def __init__(self, phase: TimingPhase, *, t: Time, name: str | None = None):
        super().__init__(t, name)
        self.phase = phase

    @override
    def invoke(self) -> None:
        # This event is directly dispatched onto nodes without going through the scheduler
        # for performance reasons, so that the invoke() method is unused.
        raise RuntimeError


class TimingMode(ABC):
    """
    Network-wide application timing mode.
    """

    def __init__(self, name: str):
        self.name = name

    def install(self, network: "QuantumNetwork"):
        self.simulator = network.simulator
        self.network = network

    @abstractmethod
    def is_async(self) -> bool:
        """
        Determine whether the network is using ASYNC timing.
        """
        pass

    @abstractmethod
    def _is_phase(self, phase: TimingPhase, t: Time | None = None) -> bool: ...

    def is_external(self, t: Time | None = None) -> bool:
        """
        Determine whether the network is either using ASYNC timing or in an EXTERNAL phase.

        Args:
            t: If specified, also check that the timestamp is in the same phase window.
        """
        return self._is_phase(TimingPhase.EXTERNAL, t)

    def is_routing(self, t: Time | None = None) -> bool:
        """
        Determine whether the network is either using ASYNC timing or in a ROUTING phase.

        Args:
            t: If specified, also check that the timestamp is in the same phase window.
        """
        return self._is_phase(TimingPhase.ROUTING, t)

    def is_internal(self, t: Time | None = None) -> bool:
        """
        Determine whether the network is either using ASYNC timing or in an INTERNAL phase.

        Args:
            t: If specified, also check that the timestamp is in the same phase window.
        """
        return self._is_phase(TimingPhase.INTERNAL, t)


class TimingModeAsync(TimingMode):
    """
    Asynchronous application timing mode.
    """

    def __init__(self, *, name="ASYNC"):
        super().__init__(name)

    @override
    def is_async(self) -> bool:
        return True

    @override
    def _is_phase(self, phase: TimingPhase, t: Time | None = None) -> bool:
        _ = phase, t
        return True


class TimingModeSync(TimingMode):
    """
    Synchronous application timing mode.
    """

    def __init__(
        self,
        *,
        name="SYNC",
        t_ext: float,
        t_rtg: float = 0,
        t_int: float,
    ):
        """
        Args:
            t_ext: EXTERNAL phase duration in seconds.
            t_rtg: ROUTING phase duration in seconds, defaults to 0.
            t_int: INTERNAL phase duration in seconds.
        """
        super().__init__(name)

        self.sequence = deque[tuple[TimingPhase, float]]()
        assert t_ext > 0
        self.sequence.append((TimingPhase.EXTERNAL, t_ext))
        if t_rtg > 0:
            self.sequence.append((TimingPhase.ROUTING, t_rtg))
        assert t_int > 0
        self.sequence.append((TimingPhase.INTERNAL, t_int))

        self.phase = self.sequence[-1][0]
        """Current phase."""
        self.end_time = Time.SENTINEL
        """Current phase end time (exclusive)."""

    @override
    def install(self, network: "QuantumNetwork"):
        super().install(network)
        self.end_time = self.simulator.ts
        self.simulator.add_event(func_to_event(self.simulator.ts, self.signal_phase))

    def signal_phase(self):
        this_phase = self.sequence.popleft()
        self.sequence.append(this_phase)
        phase, duration = this_phase

        self.phase = phase
        self.end_time = self.simulator.tc + duration

        self.simulator.add_event(func_to_event(self.end_time, self.signal_phase))

        log.debug(f"TIME_SYNC: signal {phase.name} phase")
        event = TimingPhaseEvent(phase, t=self.simulator.tc)
        for node in self.network.all_nodes:
            node.handle(event)

        # ======== INICIO PARCHE DE LIMPIEZA (TIME-SLOTTED NETWORK) ========
        if phase == TimingPhase.P1:
            if hasattr(self.network, "nodes"):
                for node in self.network.nodes:
                    # 1. Resetear las Memorias Cuánticas usando el método oficial
                    memory = getattr(node, "memory", None)
                    if memory:
                        qubits = getattr(memory, "qubits", [])
                        for q in qubits:
                            # reset_state() pone el estado a RAW y limpia las flags
                            q.reset_state()
                            q.path_id = None
                            q.active = None
                            q.entangled_qubit = None

                    # 2. Limpiar el Enrutador (Forwarder)
                    forwarder = getattr(node, "forwarder", None)
                    if forwarder:
                        if hasattr(forwarder, "active_swaps"):
                            forwarder.active_swaps.clear()
                        if hasattr(forwarder, "assigned_qubits"):
                            forwarder.assigned_qubits.clear()
                        # Si tiene una cola de peticiones, la borramos
                        if hasattr(forwarder, "requests"):
                            forwarder.requests.clear()
                            
                    # 3. Purgar Capa de Enlace (Apps)
                    apps = getattr(node, "apps", [])
                    for app in apps:
                        # Limpiar diccionarios o listas de qubits asignados
                        app_assigned = getattr(app, "assigned_qubits", None)
                        if isinstance(app_assigned, (dict, list)):
                            app_assigned.clear()
                        
                        # Limpiar colas de eventos/peticiones si existen
                        if hasattr(app, "queue") and hasattr(app.queue, "clear"):
                            app.queue.clear()
        # ======== FIN PARCHE DE LIMPIEZA ========


    @override
    def is_async(self) -> bool:
        return False

    @override
    def _is_phase(self, phase: TimingPhase, t: Time | None = None) -> bool:
        return self.phase is phase and (t is None or t < self.end_time)
    
    
class TimingModeSyncQCast(TimingMode):
    def __init__(self, t1: float, t2: float, t3: float, t4: float, name="SYNC_QCAST"):
        super().__init__(name)
        self.sequence = deque([
            (TimingPhase.P1, t1),
            (TimingPhase.P2, t2),
            (TimingPhase.P3, t3),
            (TimingPhase.P4, t4)
        ])
        self.phase = self.sequence[-1][0]
        self.end_time = Time.SENTINEL

    @override
    def install(self, network: "QuantumNetwork"):
        super().install(network)
        self.end_time = self.simulator.ts
        self.simulator.add_event(func_to_event(self.simulator.ts, self.signal_phase))

    def signal_phase(self):
        this_phase = self.sequence.popleft()
        self.sequence.append(this_phase)
        phase, duration = this_phase

        self.phase = phase
        self.end_time = self.simulator.tc + duration

        self.simulator.add_event(func_to_event(self.end_time, self.signal_phase))

        log.debug(f"TIME_SYNC: signal {phase.name} phase")
        event = TimingPhaseEvent(phase, t=self.simulator.tc)
        for node in self.network.all_nodes:
            node.handle(event)

        if phase == TimingPhase.P1:
            if hasattr(self.network, "nodes"):
                for node in self.network.nodes:
                    memory = getattr(node, "memory", None)
                    if memory:
                        qubits = getattr(memory, "qubits", [])
                        for q in qubits:
                            q.reset_state()
                            q.path_id = None
                            q.active = None
                            q.entangled_qubit = None

                    forwarder = getattr(node, "forwarder", None)
                    if forwarder:
                        if hasattr(forwarder, "active_swaps"):
                            forwarder.active_swaps.clear()
                        if hasattr(forwarder, "assigned_qubits"):
                            forwarder.assigned_qubits.clear()
                        if hasattr(forwarder, "requests"):
                            forwarder.requests.clear()

                    apps = getattr(node, "apps", [])
                    for app in apps:
                        app_assigned = getattr(app, "assigned_qubits", None)
                        if isinstance(app_assigned, (dict, list)):
                            app_assigned.clear()

                        if hasattr(app, "queue") and hasattr(app.queue, "clear"):
                            app.queue.clear()

    @override
    def is_async(self) -> bool:
        return False

    @override
    def _is_phase(self, phase: TimingPhase, t: Time | None = None) -> bool:
        return self.phase is phase and (t is None or t < self.end_time)
