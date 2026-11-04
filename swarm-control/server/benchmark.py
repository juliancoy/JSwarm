"""One bounded browser benchmark at a time; no background load."""
import secrets
import threading
import time

SIZE = 4 * 1024 * 1024
PAYLOAD = secrets.token_bytes(SIZE)


class Benchmark:
    def __init__(self):
        self.lock = threading.Lock()
        self.token = None
        self.until = 0
        self.remaining = {}

    def start(self):
        with self.lock:
            if time.monotonic() < self.until:
                return None
            self.token = secrets.token_urlsafe(24)
            self.until = time.monotonic() + 45
            self.remaining = {'ping': 6, 'download': 3, 'upload': 3}
            return self.token

    def claim(self, token, kind):
        with self.lock:
            if not token or token != self.token or time.monotonic() >= self.until or self.remaining.get(kind, 0) <= 0:
                return False
            self.remaining[kind] -= 1
            return True
