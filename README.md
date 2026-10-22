# pipehub-conformance

**A conformance checker for water valve controllers that software is allowed to move.**

When a voice assistant or a home server can open and close water valves, the dangerous moment is not the command; it is what
happens after the software dies. This kit pins that down as a short HTTP contract ([CONTRACT.md](CONTRACT.md)) built on one
rule: *every move away from a valve's safe state is a lease the controller ends on its own clock*. Branch pipes fall back open, so
nobody is left without water; tap valves ("outlets") fall back closed, so nothing is left running. Then it gives you a checker
that runs the contract against any controller over HTTP, and a reference controller that passes it.

- `pipehub_check.py`: 13 checks against a live controller (standard library only; tested on Python 3.11 and 3.13).
- `reference_hub.py`: a software model of a controller that keeps the contract, with nine `--broken` modes, each breaking one rule.
- `tests/test_checker.py`: proves the checker passes the reference and catches every broken mode.

It was extracted from **[Leak Hunt](https://github.com/arulmuruganmcse2024-tech/leak-hunt)**, an Alexa+ hackathon entry where an assistant switches pipes to locate a leak and fills a bath
to a number of litres. Leak Hunt's add-on talks to its controller through exactly this contract. The kit has no dependency on it.

> **Status.** Specified and tested against software only. No firmware has been written against it and no real valve has been
> checked with it. If you build a controller, this is the test you can run; please report what it finds.

## Quick start

```
# terminal 1: a reference controller (software model) with two pipes and one tap valve
python3 reference_hub.py --port 8099 --token demo --pipes kitchen1,toilet --outlets tub --travel 1.5

# terminal 2: check it
python3 pipehub_check.py http://127.0.0.1:8099 --token demo --pipes kitchen1,toilet --outlets tub --json report.json
```

Against your own controller, pass its URL, token and valve ids. **Valves will move during the run** (each check puts them back
at rest): run it on a bench rig or with the water off, never on a house someone is using. If your valves take longer than 6 s to
report a new position, raise `--travel-max`, and keep `--lease` (default 3 s) longer than your travel time.

The full output of that quick start is in [example_output.txt](example_output.txt) (13 checks, about 50 s).

## The checks

| Check | What it proves over HTTP | Contract |
| --- | --- | --- |
| C01 | A missing or wrong token is 401, and a rejected command moves nothing | R3 |
| C02 | Every declared valve is listed with a valid position and lease field | R4 |
| C03 | The run starts with every valve at rest and no lease (otherwise it stops) | R1 |
| C04 | An unknown valve is 404 | R5 |
| C05 | Malformed commands are 400 and move nothing; unknown fields are ignored | R6 |
| C06 | An outlet never opens without a lease | R10 |
| C07 | A valve reaches and reports the commanded position within the travel limit; commanding rest clears the lease | R7, R13, R15 |
| C08 | The controller caps leases itself: 300 s for a pipe, 60 s for an outlet | R9 |
| C09 | A pipe's lease ends on the controller's own clock: it reopens while the checker sends **nothing** | R8 |
| C10 | An outlet's lease ends on the controller's own clock: it closes while the checker sends nothing | R8 |
| C11 | Sending the same command again renews the lease | R12 |
| C12 | A pipe held with no lease stays closed until told otherwise | R11 |
| C13 | HTTPS, unless the controller is on this machine or `--allow-insecure-lan` is given | R16 |

During C09, C10 and C12 the checker counts its own requests and fails itself if it sent any, so a pass there cannot be caused by
the checker poking the controller.

## The checker can fail

A checker that passes everything proves nothing. `reference_hub.py --broken <mode>` breaks one rule, and the self-test runs the
full checker against each mode. Result of the last run (`python3 -m unittest discover -s tests -t .`, 7 tests, about 3 minutes):

| Broken mode | What the controller does wrong | Checks that failed |
| --- | --- | --- |
| `ignore_lease` | never ends a lease | C09, C10 |
| `outlet_open_unleased` | opens a tap with no lease | C06 |
| `no_cap` | holds whatever lease it is asked for | C08 |
| `no_auth` | accepts any token | C01 |
| `wrong_rest` | sends every valve to open when a lease ends, taps included | C10 |
| `rest_keeps_lease` | keeps the lease after being commanded back to rest | C07 |
| `unknown_is_200` | answers 200 for a valve it does not have | C04 |
| `sloppy_lease` | treats a zero or negative lease as "no lease" | C05 |
| `no_renew` | ignores a repeated command ("already there"), so the old lease stands | C11 |

Each mode failed exactly the checks listed and no others. The reference controller passed all 13.

## What a pass does not prove

- **That a valve physically closed.** Positions are self-reported. Put a flow meter in line and confirm that `closed` means no flow.
- **What happens on a power cut or brownout.** Pull the power mid-lease: outlets must end closed (normally-closed valves), and the
  controller must boot to rest.
- **That the lease timer is independent of the network.** C09 and C10 show the controller ends a lease with nothing sent; pull the
  network cable mid-lease on the bench to show the timer is not tied to the connection.
- **Anything about pressure, water hammer, potable-water ratings or local plumbing rules.**
- **TLS details.** C13 checks the scheme, not the certificate.
- **Behaviour under concurrent clients** or long runs.

A bench checklist for those, in order: board with LEDs instead of valves (boot state, lease on a stopwatch, network pulled); one
valve on a bucket line at low pressure (travel time, `moving` then `closed`, a jammed valve reports `fault`); a flow meter in line
(position truthfulness); two valves (independent leases); only then a real room, with the hand valves in reach.

## Why it matters

A lease turns "the software must never fail" into "the software may fail, and the water still ends in a safe state". It also makes
a promise an assistant can keep out loud: "if I lose contact, your controller turns it back on by itself within two minutes" is
true only if the controller ends the lease, and that is what C09 and C10 check. The same pattern fits any assistant action on the
physical world that must end safely without the assistant: heating, irrigation, a garage door held open.

## Licence

MIT, see [LICENSE](LICENSE).
