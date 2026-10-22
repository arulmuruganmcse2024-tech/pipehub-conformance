#!/usr/bin/env python3
"""pipehub-check: run the leased-valve contract (CONTRACT.md) against a water valve controller over HTTP.

    python3 pipehub_check.py http://127.0.0.1:8099 --token demo --pipes kitchen1,toilet --outlets tub

It sends real commands: valves WILL move during the run (each check puts them back at rest). Run it on a bench rig, never on a
house someone is using. Passing means the controller kept the contract over HTTP during this run. It does not prove that a valve
physically closed (positions are self-reported), what happens on a power cut, or anything about pressure or water hammer;
see "What a pass does not prove" in README.md.

Standard library only. Exit code 0 when every check passes or is skipped, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

POSITIONS = {"open", "closed", "moving", "fault"}
MAX_PIPE_LEASE_S = 300.0
MAX_OUTLET_LEASE_S = 60.0
VERSION = "0.1.0"


class Skip(Exception):
    pass


class Fail(Exception):
    pass


class Hub:
    """A thin HTTP client for the contract. Every request is recorded so a check can prove it sent nothing while waiting."""

    def __init__(self, base: str, token: str, timeout: float = 5.0):
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout
        self.sent: List[Tuple[float, str, str]] = []

    def call(self, method: str, path: str, body=None, token: Optional[str] = "default", raw: Optional[bytes] = None):
        tok = self.token if token == "default" else token
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if tok is not None:
            req.add_header("Authorization", f"Bearer {tok}")
        self.sent.append((time.monotonic(), method, path))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                code, text = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, text = e.code, e.read()
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise Fail(f"{method} {path}: no answer ({e})")
        try:
            return code, (json.loads(text) if text else None)
        except ValueError:
            return code, None

    def pipe(self, pid: str) -> dict:
        code, j = self.call("GET", f"/api/v1/pipes/{quote(pid)}")
        if code != 200 or not isinstance(j, dict):
            raise Fail(f"GET {pid}: HTTP {code}")
        return j

    def put(self, pid: str, want: str, lease_s, **k):
        return self.call("PUT", f"/api/v1/pipes/{quote(pid)}", {"want": want, "lease_s": lease_s}, **k)


class Run:
    def __init__(self, hub: Hub, pipes: List[str], outlets: List[str], lease_s: float, travel_max_s: float, slack_s: float):
        self.hub, self.pipes, self.outlets = hub, pipes, outlets
        self.lease_s, self.travel, self.slack = lease_s, travel_max_s, slack_s

    def rest(self, pid: str) -> str:
        return "closed" if pid in self.outlets else "open"

    def away(self, pid: str) -> str:
        return "open" if pid in self.outlets else "closed"

    def wait_for(self, pid: str, want: str, limit: Optional[float] = None) -> float:
        """Poll until the measured position equals `want`; return the seconds it took."""
        t0, limit = time.monotonic(), self.travel if limit is None else limit
        while True:
            p = self.hub.pipe(pid)
            if p.get("position") == want:
                return time.monotonic() - t0
            if p.get("position") == "fault":
                raise Fail(f"{pid} reports fault while moving to {want}")
            if time.monotonic() - t0 > limit:
                raise Fail(f"{pid} did not reach {want} within {limit:.1f} s (last position {p.get('position')!r})")
            time.sleep(0.2)

    def to_rest(self, pid: str) -> None:
        code, _ = self.hub.put(pid, self.rest(pid), None)
        if code != 200:
            raise Fail(f"could not put {pid} back at rest: HTTP {code}")
        self.wait_for(pid, self.rest(pid))

    def assert_at_rest(self, pid: str, why: str) -> None:
        p = self.hub.pipe(pid)
        if p.get("position") != self.rest(pid) or p.get("lease_s_left") is not None:
            raise Fail(f"{why}: {pid} is {p.get('position')!r} with lease {p.get('lease_s_left')!r}, expected {self.rest(pid)!r} and no lease")

    def silent(self, seconds: float) -> None:
        """Send nothing at all for `seconds`: whatever happens now is the controller's own doing."""
        n = len(self.hub.sent)
        time.sleep(seconds)
        if len(self.hub.sent) != n:
            raise Fail("the checker itself sent a request while it was meant to be silent")


# ---------------------------------------------------------------------------------------------------------------- the checks
CHECKS: List[Tuple[str, str, Callable[[Run], str]]] = []


def check(cid: str, title: str):
    def deco(fn):
        CHECKS.append((cid, title, fn))
        return fn
    return deco


@check("C01", "Rejects a missing or wrong token with 401, and a rejected command moves nothing")
def c01(r: Run) -> str:
    for tok, name in ((None, "no token"), ("wrong-" + r.hub.token, "a wrong token")):
        code, _ = r.hub.call("GET", "/api/v1/pipes", token=tok)
        if code != 401:
            raise Fail(f"GET with {name}: HTTP {code}, expected 401")
    p = r.pipes[0]
    code, _ = r.hub.put(p, r.away(p), 30, token="wrong-" + r.hub.token)
    if code != 401:
        raise Fail(f"PUT with a wrong token: HTTP {code}, expected 401")
    time.sleep(min(1.0, r.travel))
    r.assert_at_rest(p, "after a PUT with a wrong token")
    return "401 for no token and a wrong token; nothing moved"


@check("C02", "Lists every declared valve with a measured position and a lease field")
def c02(r: Run) -> str:
    code, j = r.hub.call("GET", "/api/v1/pipes")
    if code != 200 or not isinstance(j, dict) or not isinstance(j.get("pipes"), list):
        raise Fail(f"GET /api/v1/pipes: HTTP {code}, body {str(j)[:120]}")
    rows = {x.get("id"): x for x in j["pipes"] if isinstance(x, dict)}
    missing = [p for p in r.pipes + r.outlets if p not in rows]
    if missing:
        raise Fail(f"not listed: {', '.join(missing)}")
    for pid, x in rows.items():
        if x.get("position") not in POSITIONS:
            raise Fail(f"{pid}: position {x.get('position')!r} is not one of {sorted(POSITIONS)}")
        left = x.get("lease_s_left", "absent")
        if left == "absent" or not (left is None or (isinstance(left, (int, float)) and not isinstance(left, bool) and left >= 0)):
            raise Fail(f"{pid}: lease_s_left {left!r} must be null or a non-negative number")
    return f"{len(rows)} valves listed"


@check("C03", "Every declared valve starts at rest: pipes open, outlets closed, no leases")
def c03(r: Run) -> str:
    for pid in r.pipes + r.outlets:
        r.assert_at_rest(pid, "at the start")
    return "all at rest"


@check("C04", "An unknown valve is 404")
def c04(r: Run) -> str:
    code, _ = r.hub.call("GET", "/api/v1/pipes/no-such-valve-xyz")
    if code != 404:
        raise Fail(f"GET unknown valve: HTTP {code}, expected 404")
    code, _ = r.hub.put("no-such-valve-xyz", "closed", 10)
    if code != 404:
        raise Fail(f"PUT unknown valve: HTTP {code}, expected 404")
    return "404 for GET and PUT"


@check("C05", "Malformed commands are 400 and move nothing")
def c05(r: Run) -> str:
    p = r.pipes[0]
    path = f"/api/v1/pipes/{quote(p)}"
    bad = [({}, "no want"), ({"want": "sideways"}, "want sideways"), ({"want": "closed", "lease_s": -5}, "negative lease"),
           ({"want": "closed", "lease_s": 0}, "zero lease"), ({"want": "closed", "lease_s": "10"}, "lease as text"),
           ({"want": "closed", "lease_s": True}, "lease as a boolean")]
    for body, name in bad:
        code, _ = r.hub.call("PUT", path, body)
        if code != 400:
            raise Fail(f"{name}: HTTP {code}, expected 400")
    code, _ = r.hub.call("PUT", path, raw=b"{not json")
    if code != 400:
        raise Fail(f"invalid JSON: HTTP {code}, expected 400")
    time.sleep(min(1.0, r.travel))
    r.assert_at_rest(p, "after malformed commands")
    code, _ = r.hub.call("PUT", path, {"want": "open", "lease_s": None, "note": "unknown fields are ignored"})
    if code != 200:
        raise Fail(f"a valid rest command with an extra field: HTTP {code}, expected 200 (unknown fields are ignored)")
    return f"{len(bad) + 1} malformed bodies refused, nothing moved; an unknown field was ignored"


@check("C06", "An outlet never opens without a lease")
def c06(r: Run) -> str:
    if not r.outlets:
        raise Skip("no outlet declared")
    o = r.outlets[0]
    code, _ = r.hub.put(o, "open", None)
    if code != 400:
        if code == 200:
            r.to_rest(o)
        raise Fail(f"PUT open without lease_s: HTTP {code}, expected 400")
    time.sleep(min(1.0, r.travel))
    r.assert_at_rest(o, "after an unleased open")
    return "400; the outlet stayed closed"


@check("C07", "Moves to the commanded position and reports it within the travel limit; commanding rest clears the lease")
def c07(r: Run) -> str:
    out = []
    for pid in r.pipes[:1] + r.outlets[:1]:
        code, j = r.hub.put(pid, r.away(pid), 30)
        if code != 200 or not isinstance(j, dict) or j.get("position") not in (r.away(pid), "moving", r.rest(pid)):
            raise Fail(f"PUT {pid} {r.away(pid)}: HTTP {code}, body {str(j)[:120]}")
        t_away = r.wait_for(pid, r.away(pid))
        left = r.hub.pipe(pid).get("lease_s_left")
        if not isinstance(left, (int, float)) or left <= 0:
            raise Fail(f"{pid} is {r.away(pid)} on a 30 s lease but reports lease_s_left {left!r}")
        code, _ = r.hub.put(pid, r.rest(pid), None)
        if code != 200:
            raise Fail(f"PUT {pid} back to rest: HTTP {code}")
        t_back = r.wait_for(pid, r.rest(pid))
        r.assert_at_rest(pid, "after commanding rest")
        code, _ = r.hub.put(pid, r.rest(pid), None)              # idempotent
        if code != 200:
            raise Fail(f"repeating the rest command on {pid}: HTTP {code}")
        r.assert_at_rest(pid, "after repeating the rest command")
        out.append(f"{pid} {t_away:.1f} s there, {t_back:.1f} s back")
    return "; ".join(out)


@check("C08", "Caps leases itself: at most 300 s for a pipe, 60 s for an outlet")
def c08(r: Run) -> str:
    out = []
    for pid, cap in [(p, MAX_PIPE_LEASE_S) for p in r.pipes[:1]] + [(o, MAX_OUTLET_LEASE_S) for o in r.outlets[:1]]:
        code, _ = r.hub.put(pid, r.away(pid), 99999)
        if code != 200:
            raise Fail(f"PUT {pid} with lease_s 99999: HTTP {code} (the controller should cap it, not refuse it)")
        left = r.hub.pipe(pid).get("lease_s_left")
        r.to_rest(pid)
        if not isinstance(left, (int, float)) or left > cap + 0.5:
            raise Fail(f"{pid}: asked for 99999 s, holds {left!r} s; the cap is {cap:.0f} s")
        out.append(f"{pid} held {left:.0f} s")
    return "; ".join(out)


def _expiry(r: Run, pid: str) -> str:
    code, _ = r.hub.put(pid, r.away(pid), r.lease_s)
    if code != 200:
        raise Fail(f"PUT {pid} {r.away(pid)} with lease_s {r.lease_s}: HTTP {code}")
    t0 = time.monotonic()
    r.wait_for(pid, r.away(pid), limit=r.travel)
    remaining = r.lease_s - (time.monotonic() - t0)
    r.silent(max(0.0, remaining) + r.travel + r.slack)
    p = r.hub.pipe(pid)
    if p.get("position") != r.rest(pid):
        r.to_rest(pid)
        raise Fail(f"{pid} was still {p.get('position')!r} {r.lease_s + r.travel + r.slack:.1f} s after a {r.lease_s:.1f} s lease, "
                   f"with nothing sent in between; it should have gone back to {r.rest(pid)!r} on its own clock")
    if p.get("lease_s_left") is not None:
        raise Fail(f"{pid} is back at rest but still reports lease_s_left {p.get('lease_s_left')!r}")
    return f"{pid} back to {r.rest(pid)} on its own after a {r.lease_s:.1f} s lease, with nothing sent"


@check("C09", "A pipe's lease ends on the controller's own clock: it reopens with nothing sent")
def c09(r: Run) -> str:
    return _expiry(r, r.pipes[0])


@check("C10", "An outlet's lease ends on the controller's own clock: it closes with nothing sent")
def c10(r: Run) -> str:
    if not r.outlets:
        raise Skip("no outlet declared")
    return _expiry(r, r.outlets[0])


@check("C11", "Sending the same command again renews the lease")
def c11(r: Run) -> str:
    pid = r.pipes[0]
    L = max(2.0, r.lease_s)
    code, _ = r.hub.put(pid, r.away(pid), L)
    if code != 200:
        raise Fail(f"PUT {pid}: HTTP {code}")
    r.wait_for(pid, r.away(pid))
    time.sleep(L * 0.6)
    code, _ = r.hub.put(pid, r.away(pid), L)                  # renewal: same want, fresh lease
    if code != 200:
        raise Fail(f"renewal PUT {pid}: HTTP {code}")
    left = r.hub.pipe(pid).get("lease_s_left")
    if not isinstance(left, (int, float)) or left < L * 0.8:
        r.to_rest(pid)
        raise Fail(f"after renewing a {L:.1f} s lease, lease_s_left is {left!r}; a renewal must restart it")
    time.sleep(L * 0.6)                                       # past the original end, inside the renewed one
    pos = r.hub.pipe(pid).get("position")
    r.to_rest(pid)
    if pos != r.away(pid):
        raise Fail(f"{pid} went back to rest at the original lease end despite the renewal")
    return f"renewed at {L * 0.6:.1f} s; still {r.away(pid)} past the original end"


@check("C12", "A pipe held with no lease stays closed until it is told to open")
def c12(r: Run) -> str:
    pid = r.pipes[0]
    code, _ = r.hub.put(pid, "closed", None)
    if code != 200:
        raise Fail(f"PUT {pid} closed with lease_s null: HTTP {code}")
    r.wait_for(pid, "closed")
    r.silent(r.lease_s + r.slack)
    p = r.hub.pipe(pid)
    r.to_rest(pid)
    if p.get("position") != "closed" or p.get("lease_s_left") is not None:
        raise Fail(f"held pipe reported {p.get('position')!r} with lease {p.get('lease_s_left')!r} after {r.lease_s + r.slack:.1f} s")
    return "stayed closed with no lease, reopened on request"


@check("C13", "Transport: HTTPS, unless the controller is on this machine or --allow-insecure-lan is given")
def c13(r: Run) -> str:
    u = urlparse(r.hub.base)
    if u.scheme == "https":
        return "https"
    if u.hostname in ("127.0.0.1", "localhost", "::1"):
        return "plain HTTP on localhost (allowed for testing)"
    if getattr(r, "allow_insecure_lan", False):
        return "plain HTTP on a LAN, allowed by --allow-insecure-lan"
    raise Fail(f"{u.scheme}:// to {u.hostname}: the contract requires HTTPS off this machine")


# ---------------------------------------------------------------------------------------------------------------- runner
def _restore(r: Run) -> None:
    """Best effort: command every declared valve back to rest (pipes open, outlets closed)."""
    for pid in r.pipes + r.outlets:
        try:
            r.hub.put(pid, r.rest(pid), None)
        except Fail:
            pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < r.travel:
        try:
            if all(r.hub.pipe(pid).get("position") == r.rest(pid) for pid in r.pipes + r.outlets):
                return
        except Fail:
            return
        time.sleep(0.2)


def run_checks(base: str, token: str, pipes: List[str], outlets: List[str], *, lease_s: float = 3.0, travel_max_s: float = 6.0,
               slack_s: float = 1.5, only: Optional[List[str]] = None, allow_insecure_lan: bool = False, out=print) -> Dict:
    if not pipes:
        raise SystemExit("declare at least one branch pipe with --pipes")
    r = Run(Hub(base, token), pipes, outlets, lease_s, travel_max_s, slack_s)
    r.allow_insecure_lan = allow_insecure_lan
    rows, t_all = [], time.monotonic()
    for cid, title, fn in CHECKS:
        if only and cid not in only:
            continue
        t0 = time.monotonic()
        try:
            detail, status = fn(r), "PASS"
        except Skip as e:
            detail, status = str(e), "SKIP"
        except Fail as e:
            detail, status = str(e), "FAIL"
        except Exception as e:                                   # a checker bug must not look like a pass
            detail, status = f"checker error: {type(e).__name__}: {e}", "FAIL"
        rows.append(dict(id=cid, title=title, status=status, detail=detail, seconds=round(time.monotonic() - t0, 1)))
        out(f"{status:4s}  {cid}  {title}\n        {detail}")
        if cid == "C03" and status == "FAIL":
            out("        (the controller is not at rest, so the remaining checks would measure someone else's state; stopping)")
            break
        if status == "FAIL":
            _restore(r)                                          # a failed check may leave a valve away from rest
    _restore(r)
    failed = [x["id"] for x in rows if x["status"] == "FAIL"]
    rep = dict(tool="pipehub-check", version=VERSION, controller=base, pipes=pipes, outlets=outlets, lease_s=lease_s,
               travel_max_s=travel_max_s, checks=rows, failed=failed, passed=not failed, seconds=round(time.monotonic() - t_all, 1))
    out(f"\n{'PASSED' if not failed else 'FAILED: ' + ', '.join(failed)}  ({len(rows)} checks, {rep['seconds']} s)")
    return rep


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run the leased-valve contract against a controller (valves will move).")
    ap.add_argument("url", help="base URL of the controller, e.g. https://hub.local:8443")
    ap.add_argument("--token", required=True, help="bearer token")
    ap.add_argument("--pipes", required=True, help="comma-separated branch pipe ids (rest open)")
    ap.add_argument("--outlets", default="", help="comma-separated outlet ids (rest closed), if any")
    ap.add_argument("--lease", type=float, default=3.0, help="lease used by the expiry checks, seconds (default 3)")
    ap.add_argument("--travel-max", type=float, default=6.0, help="longest a valve may take to report its new position (default 6)")
    ap.add_argument("--slack", type=float, default=1.5, help="extra seconds allowed after a lease ends (default 1.5)")
    ap.add_argument("--only", default="", help="comma-separated check ids, e.g. C09,C10")
    ap.add_argument("--allow-insecure-lan", action="store_true", help="accept plain HTTP to a LAN address")
    ap.add_argument("--json", default="", help="write the report as JSON to this file")
    a = ap.parse_args(argv)
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
    rep = run_checks(a.url, a.token, split(a.pipes), split(a.outlets), lease_s=a.lease, travel_max_s=a.travel_max, slack_s=a.slack,
                     only=split(a.only) or None, allow_insecure_lan=a.allow_insecure_lan)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rep, f, indent=1)
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
