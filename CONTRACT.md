# The leased-valve contract

A small HTTP contract for a controller that opens and closes water valves on behalf of software (a voice assistant, a home
automation server, a leak locator). Its one idea: **every move away from a valve's safe state is a lease that the controller ends
on its own clock.** If the software, the network or the server dies, every valve goes back to its safe state by itself.

Each requirement names the check in `pipehub_check.py` that tests it. Requirements marked *bench* cannot be checked over HTTP and
need a person with the hardware (see README.md).

## Valves and their rest position

- **R1** Every valve has a rest position, its safe state. A **branch pipe** rests **open**, so nobody is left without water. An
  **outlet** (a tap valve something fills from) rests **closed**, so nothing is left running. *(C03)*
- **R2** At boot every valve is at rest and no lease exists. *(bench; C03 checks the state at the start of a run)*

## Requests

All requests carry `Authorization: Bearer <token>`. JSON in and out.

```
GET  /api/v1/pipes          -> {"pipes": [{"id", "position", "lease_s_left"}, ...]}
GET  /api/v1/pipes/{id}     -> {"id", "position", "lease_s_left"}
PUT  /api/v1/pipes/{id}     {"want": "open" | "closed", "lease_s": number | null}
                            -> {"id", "position", "lease_s_left"}
```

- **R3** A missing or wrong token is `401`, and a rejected command moves nothing. *(C01)*
- **R4** The listing contains every valve. `position` is one of `open`, `closed`, `moving`, `fault`; `lease_s_left` is `null` or
  a non-negative number. *(C02)*
- **R5** An unknown valve is `404`. *(C04)*
- **R6** A malformed body is `400` and moves nothing: `want` missing or not `open`/`closed`; `lease_s` present but not a positive
  number (zero, negative, text, boolean); invalid JSON. Unknown fields are ignored. *(C05)*

## Leases

- **R7** `want` away from rest with `lease_s` means: hold it there for at most `lease_s` seconds, then go back to rest. *(C07)*
- **R8** **The controller ends the lease with its own clock.** Nothing has to be sent for the valve to go back: a pipe reopens,
  an outlet closes. *(C09, C10; bench: on a hardware timer, with the network cable pulled)*
- **R9** The controller caps `lease_s` itself and never trusts the caller: at most **300 s** for a pipe, **60 s** for an outlet.
  A larger request is capped, not refused. *(C08)*
- **R10** **An outlet never opens without a lease**: `want: "open"` on an outlet with `lease_s: null` is `400`. *(C06)*
- **R11** For a pipe, `lease_s: null` means hold it closed until told otherwise. This is for a person's explicit, lasting decision
  ("keep that pipe off until it's repaired"), never for software acting on its own. *(C12)*
- **R12** Sending the same `want` again with a new `lease_s` restarts the lease. This is how software renews it while it still has
  good data, and stops renewing when it does not. *(C11)*
- **R13** `want` equal to the rest position clears any lease. `PUT` is idempotent. *(C07)*

## Position

- **R14** `position` is **measured** (limit switch, hall sensor, current signature), not the last command. A controller that cannot
  measure must not report `closed`. While a valve travels it reports `moving`; a valve that did not arrive reports `fault`.
  *(C07 checks that the reported position reaches the commanded one within the travel limit; whether it is true needs a flow meter:
  bench)*
- **R15** A valve reports its new position within the travel limit (6 s by default). *(C07)*

## Transport

- **R16** HTTPS, except on the same machine, or on a LAN when the operator explicitly allows it. *(C13)*

## Hardware (bench only)

- **R17** An outlet valve is **closed without power** (normally closed), so a power cut also ends at rest.
- **R18** A branch pipe prefers a valve that is open without power. A motorised ball valve keeps its last position on power loss,
  so the controller must reboot to rest and the lease timer alone is trusted.
- **R19** Every valve has a manual override within reach, and the controller has a physical "all to rest" button.
- **R20** Low-voltage only, valves rated for potable water and the line pressure; slow closing or arrestors against water hammer.
  Local plumbing rules apply.
