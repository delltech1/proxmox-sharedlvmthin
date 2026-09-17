#!/usr/bin/python3
"""Non-arming model of a sanlock-to-PVE-watchdog-mux bridge.

This module deliberately cannot open /run/watchdog-mux.sock.  It models and
tests the safety state machine with an injected socket-like object.  A future
privileged wrapper must pass an already validated descriptor and explicitly
enable arming; importing or executing this file never touches a watchdog.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Protocol


class BridgeState(str, Enum):
    DISARMED = "DISARMED"
    ARMING = "ARMING"
    ARMED = "ARMED"
    LEASE_UNCERTAIN = "LEASE_UNCERTAIN"
    FENCING = "FENCING"


class SocketLike(Protocol):
    def sendall(self, data: bytes) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class LeaseEvidence:
    lockspace_healthy: bool
    resource_held: bool
    owner_matches: bool
    quorum: bool
    storage_identity: bool

    @property
    def positive(self) -> bool:
        return all((
            self.lockspace_healthy,
            self.resource_held,
            self.owner_matches,
            self.quorum,
            self.storage_identity,
        ))


class WatchdogMuxBridge:
    """Fail-closed watchdog-mux client state machine.

    Refresh is legal only with fresh, wholly positive lease evidence.  Once
    evidence becomes uncertain, this object can never return to ARMED.  This
    prevents a transient stale observation from resurrecting watchdog feeds.
    """

    REFRESH = b"1"
    MAGIC_CLOSE = b"V"

    def __init__(
        self,
        socket: SocketLike,
        stop_protected_io: Callable[[], bool],
        *,
        arm_enabled: bool = False,
    ) -> None:
        self._socket = socket
        self._stop_protected_io = stop_protected_io
        self._arm_enabled = arm_enabled
        self.state = BridgeState.DISARMED
        self.reason: Optional[str] = None

    def arm(self, evidence: LeaseEvidence) -> None:
        if self.state is not BridgeState.DISARMED:
            raise RuntimeError(f"cannot arm from {self.state.value}")
        if not self._arm_enabled:
            raise RuntimeError("arming is disabled in the research prototype")
        self.state = BridgeState.ARMING
        if not evidence.positive:
            self.state = BridgeState.LEASE_UNCERTAIN
            self.reason = "initial lease evidence is not wholly positive"
            return
        self._socket.sendall(self.REFRESH)
        self.state = BridgeState.ARMED

    def refresh(self, evidence: LeaseEvidence) -> bool:
        if self.state is not BridgeState.ARMED:
            return False
        if evidence.positive:
            self._socket.sendall(self.REFRESH)
            return True

        self.state = BridgeState.LEASE_UNCERTAIN
        self.reason = "lease, quorum, owner or storage identity became uncertain"
        self.state = BridgeState.FENCING
        # Crucially, no watchdog refresh is sent before or after this call.
        stopped = bool(self._stop_protected_io())
        if stopped:
            self.disarm_after_positive_io_stop()
        return False

    def disarm_after_positive_io_stop(self) -> None:
        if self.state is not BridgeState.FENCING:
            raise RuntimeError(f"cannot disarm from {self.state.value}")
        self._socket.sendall(self.MAGIC_CLOSE)
        self._socket.close()
        self.state = BridgeState.DISARMED

    def close_unarmed(self) -> None:
        if self.state is not BridgeState.DISARMED:
            raise RuntimeError("armed bridge must not use unarmed close")
        self._socket.close()

