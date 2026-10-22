#!/usr/bin/env python3
"""Reference controller: a software model that keeps the leased-valve contract in CONTRACT.md.

It is the model pipehub_check.py is tested against, and a starting point for firmware authors. It is NOT firmware and proves
nothing about a real microcontroller, relay, valve or water. What it does model, because a real controller must:

  * a bearer token on every request
  * a MEASURED position per pipe: open | closed | moving | fault
  * travel time between a command and the position changing
  * a lease: a valve is held away from its REST position at most `lease_s` seconds, then the HUB (on its own clock) returns
    it to rest. Branch pipes rest open; OUTLETS (a tap valve something fills from) rest closed, open only with a lease,
    and never for longer than MAX_OUTLET_LEASE_S
  * boot state: every valve at rest, no leases
  * faults for tests: a stuck valve, an unreachable hub, an HTTP error, a hub that reports a wrong position

Run it:  python3 reference_hub.py --port 8099 --token demo --pipes kitchen1,toilet --outlets tub --travel 1.5

--broken <mode> makes it break one rule on purpose, so you can see the checker catch it (tests/test_checker.py does this for
every mode): ignore_lease, outlet_open_unleased, no_cap, no_auth, wrong_rest, rest_keeps_lease, unknown_is_200, sloppy_lease,
no_renew.

Extracted from Leak Hunt (tools/pipehub_emulator.py), MIT licensed.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote

OPEN, CLOSED, MOVING, FAULT = "open", "closed", "moving", "fault"
MAX_LEASE_S = 300.0
MAX_OUTLET_LEASE_S = 60.0
BROKEN_MODES = ("ignore_lease", "outlet_open_unleased", "no_cap", "no_auth", "wrong_rest", "rest_keeps_lease", "unknown_is_200",
                "sloppy_lease", "no_renew")


class _Pipe:
    def __init__(self, rest: str = OPEN):
        self.rest = rest                       # its safe state: open for a branch pipe, closed for an outlet
        self.phys = rest                       # where the valve physically is
        self.pend: Optional[tuple] = None      # (t_done, target) while it travels
        self.lease_until: Optional[float] = None
        self.stuck = False
        self.report: Optional[str] = None      # fault injection: the hub claims this position whatever the truth is


class PipeHubEmulator:
    def __init__(self, pipes: List[str], token: str = "test-token", *, clock: Callable[[], float] = time.time,
                 travel_s: float = 0.0, on_change: Optional[Callable[[str, bool, float], None]] = None,
                 host: str = "127.0.0.1", port: int = 0, outlets: List[str] = (),
                 on_outlet: Optional[Callable[[str, bool, float], None]] = None, broken: str = ""):
        if broken and broken not in BROKEN_MODES:
            raise ValueError(f"unknown broken mode {broken!r}; choose from {', '.join(BROKEN_MODES)}")
        self.broken = broken                   # break one rule on purpose (for testing the checker); "" keeps the contract
        self.token, self.clock, self.travel_s, self.on_change, self.on_outlet = token, clock, travel_s, on_change, on_outlet
        self.pipes: Dict[str, _Pipe] = {p: _Pipe() for p in pipes}
        self.pipes.update({o: _Pipe(CLOSED) for o in outlets})
        self.lock = threading.RLock()
        self.log: List[dict] = []              # every accepted command: {t, pipe, want, lease_s}
        self.requests: List[tuple] = []        # (method, path) of everything the client sent
        self.history: List[tuple] = []         # (pipe, "closed"|"open", t) physical changes, for assertions
        self.offline = False                   # the hub drops connections
        self.http_error: Optional[int] = None  # answer every request with this status
        self.latency_s = 0.0                   # real seconds to wait before answering
        emu = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):         # quiet
                pass

            def _send(self, code: int, body: dict):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str):
                emu.requests.append((method, self.path))
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                if emu.offline:
                    self.close_connection = True
                    return
                if emu.latency_s:
                    time.sleep(emu.latency_s)
                if emu.http_error:
                    return self._send(emu.http_error, {"error": "injected"})
                if self.headers.get("Authorization") != f"Bearer {emu.token}" and emu.broken != "no_auth":
                    return self._send(401, {"error": "bad token"})
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    return self._send(400, {"error": "bad json"})
                code, out = emu.handle(method, self.path, body)
                self._send(code, out)

            def do_GET(self):
                self._handle("GET")

            def do_PUT(self):
                self._handle("PUT")

        self.httpd = ThreadingHTTPServer((host, port), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://{host}:{self.port}"
        self._th: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "PipeHubEmulator":
        self._th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._th.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------------ the hub's own logic (its clock, not the server's)
    def _apply(self, p: str, closed: bool, at: float) -> None:
        self.pipes[p].phys = CLOSED if closed else OPEN
        self.history.append((p, CLOSED if closed else OPEN, at))
        if self.pipes[p].rest == CLOSED:
            if self.on_outlet:
                self.on_outlet(p, not closed, at)
        elif self.on_change:
            self.on_change(p, closed, at)

    def settle(self, now: float) -> None:
        """Run everything that should already have happened: arrivals, then lease expiries, in time order."""
        with self.lock:
            while True:
                nxt = None
                for name, s in self.pipes.items():
                    if s.pend and s.pend[0] <= now:
                        cand = (s.pend[0], 0, name)
                        nxt = cand if nxt is None or cand < nxt else nxt
                    if s.lease_until is not None and s.lease_until <= now and self.broken != "ignore_lease":
                        cand = (s.lease_until, 1, name)
                        nxt = cand if nxt is None or cand < nxt else nxt
                if nxt is None:
                    return
                t, kind, name = nxt
                s = self.pipes[name]
                if kind == 0:
                    s.pend, target = None, s.pend[1]
                    if not s.stuck:
                        self._apply(name, target == CLOSED, t)
                else:                                              # the lease lapsed: back to rest, by the hub's own clock
                    s.lease_until = None
                    s.pend = None
                    back = OPEN if self.broken == "wrong_rest" else s.rest
                    if s.phys != back and not s.stuck:
                        self._apply(name, back == CLOSED, t)

    def position(self, name: str, now: float) -> str:
        s = self.pipes[name]
        if s.report:
            return s.report
        if s.stuck:
            return FAULT
        return MOVING if s.pend else s.phys

    def row(self, name: str, now: float) -> dict:
        s = self.pipes[name]
        left = None if s.lease_until is None else max(0.0, s.lease_until - now)
        return {"id": name, "position": self.position(name, now), "lease_s_left": left}

    def handle(self, method: str, path: str, body: Optional[dict]):
        with self.lock:
            now = self.clock()
            self.settle(now)
            parts = [unquote(x) for x in path.split("?")[0].strip("/").split("/")]
            if parts[:3] != ["api", "v1", "pipes"]:
                return 404, {"error": "no such route"}
            if len(parts) == 3 and method == "GET":
                return 200, {"pipes": [self.row(p, now) for p in self.pipes]}
            if len(parts) != 4 or parts[3] not in self.pipes:
                if self.broken == "unknown_is_200" and len(parts) == 4:
                    return 200, {"id": parts[3], "position": OPEN, "lease_s_left": None}
                return 404, {"error": "unknown pipe"}
            name = parts[3]
            if method == "GET":
                return 200, self.row(name, now)
            if method != "PUT" or not isinstance(body, dict):
                return 400, {"error": "PUT {want, lease_s}"}
            want = body.get("want")
            if want not in (OPEN, CLOSED):
                return 400, {"error": "want must be 'open' or 'closed'"}
            lease = body.get("lease_s")
            s = self.pipes[name]
            if lease is not None:
                if self.broken == "sloppy_lease" and isinstance(lease, (int, float)) and lease <= 0:
                    lease = None                                   # broken: treats a nonsense lease as "no lease"
                elif not isinstance(lease, (int, float)) or isinstance(lease, bool) or lease <= 0:
                    return 400, {"error": "lease_s must be a positive number or null"}
                if lease is not None and self.broken != "no_cap":
                    lease = min(float(lease), MAX_OUTLET_LEASE_S if s.rest == CLOSED else MAX_LEASE_S)   # the hub has the last word
            if s.rest == CLOSED and want == OPEN and lease is None and self.broken != "outlet_open_unleased":
                return 400, {"error": "an outlet opens only with lease_s"}
            self.log.append({"t": now, "pipe": name, "want": want, "lease_s": lease})
            if want == s.rest:
                if self.broken != "rest_keeps_lease":
                    s.lease_until = None
            elif self.broken == "no_renew" and s.lease_until is not None and s.phys == want:
                pass                                               # broken: "already there", so the old lease stands
            else:
                s.lease_until = None if lease is None else now + lease
            if s.stuck:
                return 200, self.row(name, now)
            if want == s.phys and not s.pend:
                return 200, self.row(name, now)
            if self.travel_s <= 0:
                s.pend = None
                self._apply(name, want == CLOSED, now)
            else:
                s.pend = (now + self.travel_s, want)
            return 200, self.row(name, now)

    # ------------------------------------------------------------------ test hooks (not part of the contract)
    def inject(self, name: str, *, stuck: Optional[bool] = None, report: Optional[str] = "keep") -> None:
        with self.lock:
            s = self.pipes[name]
            if stuck is not None:
                s.stuck = stuck
            if report != "keep":
                s.report = report

    def reboot(self) -> None:
        """Power cycle: every valve returns to its rest position (pipes open, outlets closed), leases are forgotten."""
        with self.lock:
            now = self.clock()
            for name, s in self.pipes.items():
                s.pend, s.lease_until = None, None
                if s.phys != s.rest:
                    self._apply(name, s.rest == CLOSED, now)

    def closed_pipes(self) -> List[str]:
        with self.lock:
            self.settle(self.clock())
            return sorted(n for n, s in self.pipes.items() if s.phys == CLOSED and s.rest == OPEN)

    def max_closed_run_s(self, name: str, until: Optional[float] = None) -> float:
        """Longest time `name` was physically closed (history is recorded at the time the valve actually moved)."""
        end = self.clock() if until is None else until
        longest, t_close = 0.0, None
        for p, pos, t in self.history:
            if p != name:
                continue
            if pos == CLOSED and t_close is None:
                t_close = t
            elif pos == OPEN and t_close is not None:
                longest, t_close = max(longest, t - t_close), None
        if t_close is not None:
            longest = max(longest, end - t_close)
        return longest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--token", default="demo")
    ap.add_argument("--pipes", default="kitchen1,toilet")
    ap.add_argument("--outlets", default="", help="comma-separated outlet ids (rest closed)")
    ap.add_argument("--travel", type=float, default=1.5, help="seconds a valve takes to move")
    ap.add_argument("--broken", default="", choices=("",) + BROKEN_MODES, help="break one rule on purpose")
    a = ap.parse_args()
    emu = PipeHubEmulator([p for p in a.pipes.split(",") if p], a.token, travel_s=a.travel, port=a.port,
                          outlets=[o for o in a.outlets.split(",") if o], broken=a.broken).start()
    print(f"reference controller on {emu.base_url}  token={a.token}  pipes={a.pipes}  outlets={a.outlets or '-'}"
          f"{'  BROKEN: ' + a.broken if a.broken else ''}  (software model, not firmware)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        emu.stop()


if __name__ == "__main__":
    main()
