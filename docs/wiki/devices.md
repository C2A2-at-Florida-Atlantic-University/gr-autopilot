# Radios and device health

The radios used here are ADALM-Pluto software-defined radio units, reached over a network protocol
by the `libiio` library. This page describes how their health is monitored, what happens when one
disappears, and why that case gets so much attention.

## Why device loss is a design problem

Losing a radio does not fail gracefully, and the way it fails depends on how the connection ended.
Both cases below were measured on the bench, by proxying a radio's connection through the local
machine and then interrupting it.

| How the connection ends | What happens |
|---|---|
| Closed cleanly | A warning is logged, the source stops producing, the process survives |
| Goes **silent** | The library throws from one of its own threads; nothing catches it; the process is terminated with `SIGABRT` and a core dump |

**Unplugging a cable produces the second case.** The network interface disappears and packets stop
arriving; nothing sends a reset. No exception handler can intervene, because the process is killed
by the runtime rather than raising anything.

This is why the radios live in a separate process. See [system
architecture](/wiki/architecture).

## How liveness is checked

One attribute read per radio, over a separate short-lived connection, costing a few milliseconds
and taking no samples from any capture in progress.

Two apparently sensible alternatives are unsafe, and both were measured rather than assumed:

- **Capturing samples over a second connection** succeeds — the device server multiplexes it —
  while silently taking two entire buffers away from the capture already running. A health check
  that corrupts the measurement it is meant to be watching is worse than no health check.
- **The stand-alone health script writes** tuning frequency, gain mode, sample rate and gain. Used
  as a periodic probe it would overwrite whatever the experiment had just configured, and the
  resulting bad measurement would be indistinguishable from physics.

## What you see when a radio is lost

Ask the portal:

```bash
curl -s http://127.0.0.1:8080/devices
```

With everything healthy:

```json
{"devices": {"tx": {"uri": "ip:192.168.2.1", "alive": true, "temp_c": 38.6},
             "rx": {"uri": "ip:192.168.3.1", "alive": true, "temp_c": 37.7}},
 "worker": {"running": true, "pid": 183136, "reason": "ready"}}
```

After the receiver is disconnected:

```json
{"devices": {"tx": {"uri": "ip:192.168.2.1", "alive": true,  "temp_c": 38.6},
             "rx": {"uri": "ip:127.0.0.3",   "alive": false,
                    "reason": "no answer within the probe timeout"}},
 "worker": {"running": false, "signal": "SIGABRT"}}
```

Note that the **transmitter is still reported as healthy**. Losing one radio kills the worker that
held both, but the other device is usually fine, and reporting both as gone would blank a working
device and misdescribe the fault. While the worker is alive it answers health queries, because it
is the process holding the radios; once it has died, each device is probed independently.

The state is measured on every request, never cached. A value remembered from startup would keep
asserting a radio is present after it had gone.

## What the agent sees

A measurement attempt against a missing radio returns an error, not a poor result:

```json
{"error": {"code": "device_unavailable",
           "message": "worker aborted (SIGABRT) — the input/output library terminates the
                       process when a radio's connection goes silent, which is what an
                       unplugged cable looks like"}}
```

This is deliberately distinct from a measurement failure. A link measured as poor is a *result*;
a missing radio is the *absence* of one. If the two were the same, a controller could not tell
"this modulation is too ambitious" from "the receiver is gone", and would respond to a cable
fault by lowering the data rate.

Equally deliberately, an error caused by a faulty **request** is a third thing again, and is not
reported as device loss. Mislabelling a defect as a hardware problem would hide real bugs behind a
plausible excuse and write spurious device-loss entries into the record.

## What the dashboard does

- Each radio gets a tile showing its live state and temperature, turning red with the reason when
  it stops answering.
- The constellation and spectrum are **removed** if the receiver is lost, replaced by a statement
  that the receiver is unavailable. Both plots are made from what the receiver captured; leaving
  the last picture on screen invites reading an old capture as the present.
- The device tiles sit above the idle/active split, because losing a receiver stops the run, and
  that is precisely the moment you need to see *which* radio went rather than a general notice
  that nothing is running.

## Recovering

Plug the radio back in and restart the worker. The daemon does not need restarting — it never
stopped. The experiment resumes with its history intact, including the aborted iteration marking
where the interruption happened.

## The bench declaration

Device detection alone is a single source of truth about the hardware, and a single source cannot
be contradicted. That failed exactly as you would expect: the daemon once ran real radios for weeks
while its device tools reported fabricated manifests — the wrong chip, the wrong tuning range, the
wrong clock — and nothing could disagree with them, so the agent reasoned about radios that were
not on the bench.

`config/bench.yaml` is the second source. It declares what each radio *should* be, and the daemon
reads each unit's identity from the hardware at startup and compares. A disagreement is a **fault**:
the server still comes up, but `run_flowgraph` is refused with a `topology_fault` naming the
mismatch, because measurements taken on a bench nobody can describe are not worth having.

What the file carries falls into three tiers, and they are not equally trustworthy:

| Tier | Example | How it is established |
|---|---|---|
| A | serial, chip model, firmware, clock trim, tuning range | **Verified** against the hardware over `libiio`, in milliseconds, disturbing nothing |
| B | whether the transmitter actually reaches the receiver, and with what loss | Verifiable only by transmitting; must run through the radio worker while idle. **Not yet implemented** |
| C | attenuator values; whether this is a cable or an antenna; whether the chamber is screened | **Not verifiable by any measurement.** Carried as a dated, named operator attestation |

It also declares constraints the framework enforces at load: exactly one transmitter, one receiver,
at most one jammer; only device kinds it can actually drive; a kind may only hold a role it can
fill; and no two declared devices may share a serial. The **link band** is derived as the
intersection of the transmitter's and receiver's tuning ranges — on an asymmetric pair that is the
constraint people get wrong, and an agent asking to retune outside it is now refused with a reason
instead of getting a confusing measurement failure.

Finally it carries the **measured calibration** for this bench, which the daemon uses to start the
worker. Gains do not travel between benches: the coax values were wrong once the radios went
wireless, and a hardcoded default cannot know that.

## The operator channel

The jammer and the hidden channel condition are framework property, reachable only over
`POST /control` — which no agent tool touches, and which never reports the channel quality back.
Both sit on the operator's side of the integrity boundary for the same reason: an agent that
could see where the interference is has not adapted to anything, and an agent that could ease its
own channel has not been graded.

Arming the jammer requires the declaration to attest **containment**, which is a property of the
enclosure and not of the antennas — antennas are normal and correct inside a screened chamber, and
catastrophic outside one. *Anechoic* does not imply *shielded*: absorber suppresses reflections,
which is a measurement property, and does nothing to stop energy leaving the room. That distinction
is asked for explicitly and never inferred from the word "chamber".
