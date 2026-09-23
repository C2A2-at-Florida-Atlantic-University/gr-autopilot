# Signal-processing chains

The agent designs a link by naming an ordered list of stages. This page describes what those
names mean, how they become a running flowgraph, and how you get the result out and put an edited
version back.

## A chain is a list of stages

A specification names a modulation and two chains:

```json
{"structure_id": "qpsk_link",
 "modulation":   "qpsk",
 "tx_chain":     ["qpsk_mod", "rrc_pulse_shape"],
 "rx_chain":     ["rrc_matched_filter", "symbol_sync", "qpsk_demod"],
 "pulse_shape":  {"sps": 4, "rolloff": 0.35}}
```

`tx_chain` turns bits into a signal; `rx_chain` turns a received signal back into symbols. Each
name is a **skill**, and each skill is realised by a real GNU Radio block:

| Skill | Block | What it does |
|---|---|---|
| `bpsk_mod`, `qpsk_mod`, `qam16_mod` | `chunks_to_symbols` | Maps groups of bits to constellation points |
| `rrc_pulse_shape` | `interp_fir_filter` | Root-raised-cosine transmit filter; also raises the rate to the chosen samples per symbol |
| `rrc_matched_filter` | `fir_filter` | Matched receive filter; also decimates to two samples per symbol |
| `symbol_sync` | `symbol_sync` | Gardner symbol timing recovery; decimates to one sample per symbol |
| `costas_carrier` | `costas_loop` | Carrier frequency and phase recovery |
| `agc` | `agc2` | Automatic gain control |

The channel between the two chains belongs to the framework. The agent does not place it.

## Modulations

Every rung the framework can transmit, decide and score, in ladder order. Bits per symbol is the
spectral efficiency the controller maximises; the last column is what the Pluto receiver uses to
track the carrier, which is what decides whether a rung can be measured over the air at all.

| Modulation | Bits/symbol | Constant modulus | Pluto carrier recovery | Measured on the radios |
|---|---|---|---|---|
| BPSK | 1 | yes | Costas, order 2 | yes |
| QPSK | 2 | yes | Costas, order 4 | yes |
| 8-PSK | 3 | yes | Costas, order 8 | yes — 0 errors in 240,000 full-length bits at 18 dB transmit attenuation |
| 16-QAM | 4 | no | decision-directed loop or pilot-aided (ZC path) | yes |
| 32-QAM | 5 | no | decision-directed loop or pilot-aided | yes — 2.6 × 10⁻⁴ over three full-length trials at 18 dB attenuation |
| 64-QAM | 6 | no | decision-directed loop or pilot-aided | closes in quiet trials (three full-length grades, 0 errors in 160,000 bits, error vector 4–5%); a *sustained* link is not yet shown, see below |
| 256-QAM | 8 | no | decision-directed loop or pilot-aided | not closed (1.7 × 10⁻¹), measured only inside an unstable window — not yet a clean negative |

A Costas loop needs a constant-modulus constellation; on QAM its error signal is inconsistent
across inner and outer points and it spins the constellation into a ring. QAM rungs therefore go
through the decision-directed loop seeded by the preamble phase, or through pilot symbols. Each
rung needs roughly 3 dB more than the one below it for the same error ratio, so the ladder does
not end at the top of the list on a real link — it ends where the receiver's error-vector floor
says, and the diagnoser judges tightness against each rung's own decision distance so a healthy
BPSK cloud and a failing 64-QAM cloud are not confused for having the same spread.

What the bench measured on 2026-09-08 (chamber, 2.370 GHz, data in
`runs/mini-test-1/phase_b_*.json`): the error-vector floor is **thermal**, not an impairment —
the BPSK error vector magnitude (EVM) fell from 13.5% at the usual 38 dB transmit attenuation to 5.9% at 18 dB, and single
trials reached 3.4–4.0%. At that budget 64-QAM decoded three full-length trials with zero errors.
But the same budget made the link **intermittently unstable**: over six consecutive 64-QAM trials
the estimated signal-to-noise ratio (SNR) ran 27.9 → 26.0 → 20.7 → −2.6 → −2.6 → 19.2 dB, two of them with no usable
link at all, and 8-PSK afterwards drifted 29 → 17 dB. So the modulation ceiling of this hardware
is at least 64-QAM when the link is quiet, and the open question is the operating point — most
likely receive-side overload at a fixed 45 dB gain with 20 dB more transmit power, or thermal
drift — not the constellation. The next measurement is the same ladder at 22–26 dB attenuation
with the receive gain lowered, twenty trials per rung, with SNR spread reported beside the bit error ratio (BER).

## The chain determines the measurement

This is worth demonstrating rather than asserting. The following are the same link at the same
channel setting, differing only in the receive chain:

| Receive chain | Bit error ratio | Signal-to-noise ratio |
|---|---|---|
| Matched filter, symbol synchroniser | 1.4 × 10⁻³ | 11.6 dB |
| Symbol synchroniser only | 1.6 × 10⁻² | 6.7 dB |
| Matched filter only | 0.498 — the link fails | — |

Removing the matched filter costs about five decibels, because a filter matched to the transmit
pulse maximises the signal-to-noise ratio at the sampling instant and removing it leaves the
noise unfiltered. Removing the symbol synchroniser destroys the link outright, because nothing
downstream has a symbol grid to work with.

Carrier recovery earns its place only when there is something to recover. With the transmitter
and receiver oscillators offset by 2 kHz:

| Receive chain | Bit error ratio |
|---|---|
| Without `costas_carrier` | 0.497 — the link fails |
| With `costas_carrier` | 1.6 × 10⁻² |

## What the framework does not do for you

Grading performs **alignment only**: it searches a small range of whole-symbol offsets and fits a
single complex gain, both against the payload it already knows. It does **not** search sampling
phase.

That restraint is deliberate. Searching sampling phase would be performing timing recovery on the
agent's behalf, and a chain with no synchroniser would then appear to work — concealing exactly
the failure the agent needs to see. Instead, a chain that never reaches one sample per symbol is
reported as such:

```json
{"grading": "no symbol grid: receive chain did not decimate to 1 sample/symbol",
 "output_sps": 2.0, "synchronized": false}
```

Two stages are framework-owned and cannot be placed by the agent. The **channel** is the operator's
to set. The **symbol decision** belongs to the scoring layer, because that layer holds the known
payload and does the error counting; a chain names its demodulator so the specification can be
validated, but no block is placed for it.

## Configured by position, not by assumption

Blocks are configured from the rate that actually reaches them rather than from a fixed pipeline.
The matched filter decimates to two samples per symbol because the Gardner detector downstream
compares a sample at the symbol centre with one midway between symbols, and becomes unstable when
handed more. The synchroniser is then told the rate it is actually receiving.

This is why a chain remains correct when the agent puts stages in an unusual order, and why
omitting a stage changes the configuration of the ones that remain rather than silently
mis-configuring them.

## Taking the flowgraph away

Every structure that runs is exported automatically as a GNU Radio Companion file,
`runs/<experiment>/flowgraphs/<structure>.grc`, the first time it produces a measurement; the
portal's Flowgraphs page lists and downloads them. An existing file is never overwritten: a
different graph run under a name that is already taken is written beside it with a short content
hash appended. `export_flowgraph` re-exports the current graph on demand or to another path. Open
a file with `gnuradio-companion`, or compile it with `grcc` to get a runnable Python program.

On the executing backend (`gr-spec`, the daemon's default) the export is **faithful**: every
block written is a block the experiment actually instantiates, with the same filter taps, the same
loop bandwidth, the same decimation. An export showing a signal chain the measurement did not use
would attribute results to a structure that never ran, which is worse than exporting nothing.

On the **Pluto hardware backend that guarantee does not hold**, and the file says so in its
description. `PlutoBackend` runs a fixed tracking receiver (Gardner timing into a Costas loop
behind a framework-owned sync preamble) and reads only `modulation`, `rolloff`, `sps` and `coding`
from the specification; `tx_chain` and `rx_chain` are validated but not executed. An export taken
from a hardware run therefore records the *requested* structure, and it contains an AWGN channel
block rather than the radios, so opening it reproduces a baseband simulation of the run, not the
run. Treat it as the structure the agent asked for, and the ledger's `backend` field as the
statement of what actually measured it.

Filter taps are written as an expression — `firdes.root_raised_cosine(1.0, 4.0, 1.0, 0.35, 33)` —
rather than as a list of numbers, so changing the excess bandwidth in the editor recomputes them.

Two departures from the running experiment are unavoidable, and both are written into the file as
comments rather than left to be discovered:

- **The payload source** carries a short repeating excerpt. During an experiment it holds tens of
  thousands of known symbols, which would make the file unreadable.
- **The graph ends at recovered symbols.** The symbol decision and the error count belong to the
  scoring layer and are not blocks.

The channel's noise amplitude is written as a placeholder, not the true value. The true value is
computed from the hidden channel quality, so writing it would disclose by arithmetic exactly what
every tool response withholds.

## Editing it and handing it back

`import_flowgraph` reads a Companion file and makes it the graph to run. The file is walked from
its payload source to its sink following the **connections** rather than the order blocks appear
in the document; everything before the channel is the transmit chain and everything after it is
the receive chain.

Settings the backend honours are read back out of the block parameters, including out of the
filter-tap expression. So an edit reaches the measurement:

| | Bit error ratio |
|---|---|
| As the agent built it | 2.9 × 10⁻³ |
| After widening the timing loop by hand | 7.3 × 10⁻⁴ |

A block with no equivalent in the skill vocabulary is **refused by name**:

```
block 'mystery_1' (some_oot_module_block) has no equivalent in the skill vocabulary,
so this flowgraph cannot be run as written.
```

Silently dropping a block you deliberately added would run something other than what you asked
for and then report the result as yours.

Runs from an imported graph are recorded as hand-edited, with the file's content hash, so a
measurement produced by a graph you changed remains distinguishable from one the agent proposed:

```
it=1  verdict='run'       run qpsk
it=2  verdict='imported'  imported hand-edited flowgraph from mine.grc
it=3  verdict='run'       run qpsk
```

## The backends

Three backends exist and they answer different questions.

**`pluto`** drives two PlutoSDRs through a worker subprocess (`--radios`). It has its own fixed
receiver and honours only the modulation, excess bandwidth, samples per symbol and coding from a
specification — see the note under *Taking the flowgraph away* above. Structural edits to a chain
do not change a hardware measurement; modulation and rolloff do.

**`gr-spec`** executes the agent's chain as a real GNU Radio flowgraph. It is what the daemon uses
by default, because it is the one where structural choices — and your edits — change the
measurement.

**`numpy-sim`** applies noise at the symbol rate with no pulse shaping and no synchronisation. It
ignores samples per symbol and excess bandwidth entirely, so every chain measures the same. It
remains the reference against which error-ratio theory is checked, and the fallback when GNU Radio
is not installed, but running the portal on it would quietly undo the point of the chain being
real.

For reference, the executing backend tracks the theoretical reference closely, with the small
implementation loss a real pulse-shaped chain with real timing recovery should have:

| Es/N₀ | `gr-spec` | `numpy-sim` reference |
|---|---|---|
| 6 dB | 2.6 × 10⁻² | 2.2 × 10⁻² |
| 8 dB | 1.4 × 10⁻² | 6.1 × 10⁻³ |
| 12 dB | 1.9 × 10⁻³ | 0 |
