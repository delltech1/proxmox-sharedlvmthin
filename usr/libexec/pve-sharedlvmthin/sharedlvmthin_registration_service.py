"""Bounded read-only client for the fixed systemd inventory socket."""
import json
import socket
import threading
import time

SOCKET_PATH = '/run/pve-sharedlvmthin-registration.sock'
MAX_BYTES = 1024 * 1024


class InventoryUnavailable(RuntimeError):
    def __init__(self, message, code='unavailable'):
        super().__init__(message)
        self.code = code


def may_discover(session, permissions, required):
    if not isinstance(session, dict) or not session.get('pve_ticket') or not isinstance(permissions, dict):
        return False
    if session.get('user') == 'root@pam':
        return True
    root = permissions.get('/') if isinstance(permissions.get('/'), dict) else permissions
    return set(required).issubset({key for key, value in root.items() if value})


class InventoryClient:
    def __init__(self, path=SOCKET_PATH, connector=socket.socket, clock=time.monotonic):
        self.path, self.connector, self.clock = path, connector, clock
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0

    def observe(self, timeout=180, cache_seconds=0):
        if not self.lock.acquire(blocking=False):
            raise InventoryUnavailable('inventory already running')
        try:
            now = self.clock()
            if self.cached is not None and now - self.cached_at < cache_seconds:
                return self.cached
            deadline = now + timeout
            chunks, count = [], 0
            try:
                with self.connector(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                    conn.settimeout(max(0.1, deadline - self.clock()))
                    conn.connect(self.path)
                    while True:
                        remaining = deadline - self.clock()
                        if remaining <= 0:
                            raise InventoryUnavailable('inventory timed out')
                        conn.settimeout(remaining)
                        chunk = conn.recv(min(65536, MAX_BYTES + 1 - count))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        count += len(chunk)
                        if count > MAX_BYTES:
                            raise InventoryUnavailable('inventory response too large')
                result = json.loads(b''.join(chunks))
            except (OSError, ValueError, UnicodeError) as exc:
                raise InventoryUnavailable('inventory unavailable') from exc
            if isinstance(result, dict) and result.get('result') == 'UNKNOWN':
                reason = result.get('reason')
                if isinstance(reason, str) and reason.startswith('peer SSH proof '):
                    raise InventoryUnavailable('peer SSH proof unavailable', 'peer_ssh_unverified')
                if reason == 'cluster quorum not proven':
                    raise InventoryUnavailable('cluster quorum unavailable', 'quorum_unverified')
                if reason in ('cluster node unavailable or ambiguous', 'cluster membership incomplete'):
                    raise InventoryUnavailable('cluster membership unavailable', 'nodes_unverified')
            if (not isinstance(result, dict) or result.get('result') != 'OBSERVED'
                    or not isinstance(result.get('observed_at'), str)
                    or not isinstance(result.get('nodes'), list)
                    or not isinstance(result.get('candidates'), list)
                    or not isinstance(result.get('storage_definitions'), list)):
                raise InventoryUnavailable('inventory incomplete')
            self.cached, self.cached_at = result, self.clock()
            return result
        finally:
            self.lock.release()
