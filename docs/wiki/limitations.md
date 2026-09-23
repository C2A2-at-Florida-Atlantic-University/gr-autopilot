# Limitations

What this system does not do, stated plainly. Several of these are load-bearing: they bound what
the results mean, not merely what is convenient.

## Scope of the chain vocabulary

The agent composes from a **curated set of stages**, not from the whole of GNU Radio. Modulation
(BPSK, QPSK, 8-PSK, 16/32/64/256-QAM), pulse shaping, matched filtering, symbol timing recovery,
carrier recovery and automatic gain control are available. That is enough for the exercises in the
library and for adaptive modulation work, and it is considerably less than GNU Radio contains.

Every listed modulation is simulated and theory-checked; which of them a real radio can carry is
a separate question, answered by measurement on 2026-09-08: BPSK, QPSK, 8-PSK, 16-QAM and 32-QAM
close on the bench; 64-QAM closes in quiet full-length trials but a sustained 64-QAM link is not
yet demonstrated, because the transmit budget that reaches its error-vector requirement also
destabilises the receive chain (see [signal-processing chains](/wiki/flowgraphs)); 256-QAM has
not closed and has not yet had a clean test.

The block browser exposes several hundred blocks for reference, but the agent cannot place an
arbitrary one. Adding a block means giving it a factory and a specification entry.

Two stages are framework-owned by design and will not become agent-placeable: the **channel**,
which is the operator's, and the **symbol decision**, which belongs to the layer that counts
errors against a known payload.

## Round-tripping an edited flowgraph

Import understands the blocks in the vocabulary above. A file containing anything else — including
an out-of-tree module — is **refused by name** rather than partially executed. Silently dropping a
block you added would run something other than what you asked for.

Parameters read back are those the backend honours: samples per symbol, excess bandwidth, timing
loop bandwidth. Rearranging a graph into a topology the walk cannot follow — a branch, a feedback
path — is not supported.

## Simulation

The executing simulation applies **additive white Gaussian noise and an optional carrier frequency
offset**. There is no multipath, no fading, no phase noise, no non-linearity, no adjacent-channel
interference. Results are correspondingly idealised.

The two simulated backends answer different questions, and mixing them up would be a mistake. The
executing backend is where structure matters; the pure-numerical one is the reference against
which error-ratio theory is checked but ignores pulse shaping and synchronisation entirely.

## Hardware

The bench is **two ADALM-Pluto units and a HackRF interferer**. Since 7 Sep 2026 they are wireless
with 2.4 GHz dipoles inside a **screened anechoic chamber**; before that they were on a cabled,
20 dB-attenuated coax path. Nothing radiates into open spectrum either way.

What the move changed, measured rather than assumed:

- **Coupling is set by geometry, not by a pad.** Path loss, and the jammer-to-signal ratio with it,
  now depend on separation and antenna orientation. Results taken in the chamber are not directly
  comparable with results 1–12 from the cabled path, and the separation must be recorded for a
  chamber result to be reproducible at all.
- **The antennas set the usable band, not the radios.** The pair tunes 325 MHz–3.8 GHz; standard
  dual-band WiFi dipoles are efficient only near 2.4 GHz. Measured at fixed power: 2320–2400 MHz
  all within 1.3 dB of each other, then −3.6 dB by 2500 and −8.6 dB by 2550.
- **The chamber is not empty.** It hosts a second, unrelated experiment radiating WiFi and
  Bluetooth across 2402–2480 MHz, so the link runs **below ISM on 2350–2390 MHz**. A link inside a
  20 MHz WiFi channel measured ~8 dB worse than one below its lower edge, while instantaneous
  energy detection called that channel clear — a single-shot detector cannot see bursty wideband
  traffic between packets.

Consequently there is still no antenna *pattern* study and no propagation beyond a short indoor
line-of-sight path, but "no external interference" no longer holds: the ambient is real, and
experiments must be sited around it.

Frequency hopping demonstrates the **mechanism** — a link out-hopping an interferer that follows
it — and the margin is bounded by tooling rather than by physics. The link retunes far faster than
the interferer, which is a real asymmetry but a property of the equipment rather than a general
result. An interferer that reacts within a single dwell wins, and the system will show that
happening.

## Protocol

The HTTP transport answers each request with a single reply. It does not offer the
server-initiated event stream the specification also permits, so a long tool call blocks until it
completes, with no progress reporting and no cancellation. A parameter-tuning run of a dozen
trials is a dozen measurements inside one request.

One agent session is assumed at a time. Nothing prevents a second client connecting, but they
would share one service and one set of records.

## Records

The imported pre-existing history is **one experiment of 4,190 iterations**, not the many sessions
that actually produced it. The original format recorded no run boundaries, so the divisions cannot
be recovered; inventing them would fabricate history. Records made from now on have real
boundaries.

A corrupted database file is not partially readable the way the previous append-only text log was.
Completed writes are atomic, which is stronger; the file as a whole is more brittle, which is
weaker. This was a deliberate exchange.

## What has been measured, and what has been assumed

Claims about device-loss behaviour in this documentation were measured on the bench, by proxying a
radio's connection and interrupting it in two distinct ways. The distinction between a cleanly
closed connection (survivable) and a silent one (fatal to the holding process) is a measurement,
not an inference.

The claim that an **unplugged cable** produces the silent case is a reasoned inference from how a
network interface disappears, not a measurement of a physically unplugged device. It is consistent
with everything observed, and it is the assumption the architecture rests on.

## Not yet built

- Progress reporting and cancellation for long tool calls, which need the event-stream transport.
- Multi-user or authenticated operation beyond a single bearer token on the loopback interface.
- An operator control surface on the daemon: the hidden condition is currently set at startup or
  through a separate path, not through a channel the running server exposes.
- Automatic restart of a radio worker after a device returns; recovery is currently manual.

## Interferer results before 2026-09-08

The CLI (`hackrf_transfer`) jammer transmitted a ~16 %-duty pulsed tone rather than a continuous
CW until 2026-09-08 (the tone file was shorter than one USB transfer). Interferer results measured
on the bench before that date are to be re-measured against a continuous tone. The startup
path check (`--verify-path`) now senses the keyed jammer three times and refuses a spread over
6 dB, so a pulsed or intermittent interferer is named at start rather than averaged into a result.

## Measurement caveats found 2026-09-08

- **`capture_spectrum` and the dashboard's spectrum panels are symbol-domain, not
  radio-frequency (RF).** Both transform the recovered symbols at one sample per
  symbol, after the matched filter and timing recovery, so the pulse shaping is already gone and
  the axis spans the symbol rate. They show where energy sits relative to the carrier; they cannot
  show occupied bandwidth or excess bandwidth. A true RF spectrum needs the raw receive capture,
  which `TelemetryWriter` already accepts (`rx_capture`) and no backend yet supplies.
- **On the Pluto backend roughly a third of trials grade fewer bits than requested**, and in a
  117-trial soak every bit error observed fell in one of those short trials while the full-length
  trials — same error vector magnitude (EVM), same signal-to-noise ratio (SNR) — carried none. The short grade is
  an alignment artefact, not a channel event. Read every bit error ratio (BER) beside its `n_bits`,
  prefer full-length trials when bounding an error ratio, and treat a short trial's errors as
  suspect until the cause is fixed.
