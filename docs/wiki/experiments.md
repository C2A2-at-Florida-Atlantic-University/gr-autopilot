# Experiments and their records

Everything the system does is recorded, so that a result can be traced back to the conditions and
the structure that produced it. This page describes the record and how to read it.

## The model

```
experiment          one investigation: a goal, a backend, a start and end time
  ├── iteration     one measured step: what changed, what was measured
  └── artifact      a file it produced, such as an exported flowgraph
```

An **experiment** is an investigation. An **iteration** is one recorded step within it. An
**artifact** is a file that experiment produced.

Records are kept in a SQLite database — a single local file, part of the Python standard library,
requiring no server and no dependency.

## Reading the record

Through the portal:

```bash
curl -s http://127.0.0.1:8080/experiments
```

```json
{"experiments": [{
   "id": 1, "name": "session", "iterations": 3, "best_ber": 0.0, "running": true,
   "artifacts": [{"kind": "flowgraph.grc", "iteration_seq": 2,
                  "meta": {"modulation": "qpsk",
                           "tx_chain": ["qpsk_mod", "rrc_pulse_shape"],
                           "rx_chain": ["rrc_matched_filter", "symbol_sync", "qpsk_demod"]}}]}]}
```

The agent reads the same history through `read_edit_ledger`, rendered as a compact table, which is
how it remembers what it has already tried within a session.

## What an iteration records

| Field | Meaning |
|---|---|
| `seq` | Position within the experiment, starting at 1 |
| `loop` | `outer` for a structural change, `inner` for parameter tuning |
| `structure_id` | Which flowgraph structure was running |
| `edit` | What changed |
| `verdict` | `run`, `imported`, `annotation`, `aborted:device_lost`, … |
| `metrics` | The measurements, or empty if there were none |
| `note` | Free text, usually the agent's reasoning |

Bit error ratio, error vector magnitude and signal-to-noise ratio are also stored in their own
columns, so questions such as *what was the best result in this experiment* are answered by an
indexed query rather than by reading and parsing everything.

## Aborted iterations

An iteration where a radio disappeared is recorded with an empty measurement and the verdict
`aborted:device_lost`:

```
it=1  verdict='run'                  metrics={'BER': 0.0015, 'EVM': 9.2, 'SNR_est': 20.7}
it=2  verdict='aborted:device_lost'  metrics={}
```

This is not a technicality. A stalled receiver still returns buffers, and those buffers grade as
an error ratio near one half — indistinguishable from severe interference. Recording that would
place a fabricated measurement in the record. Reporting it to the agent as a poor result would be
worse: the reasonable response to a suddenly terrible channel is to lower the data rate, which
does nothing whatsoever about a disconnected cable. See [radios and device
health](/wiki/devices).

## Annotations

`annotate_ledger` attaches a note to an earlier iteration. Annotations are recorded as their own
rows carrying the sequence number of the iteration they refer to, so the original measurement is
untouched and the reasoning sits alongside it in order.

## Concurrency

Write-ahead logging is enabled, so a reader — the dashboard, polling several times a second —
never blocks the writer appending an iteration. A busy timeout is set because more than one
process may legitimately hold the database open. Connections are created per thread, because this
interpreter's SQLite module reports a thread-safety level at which a single connection must not be
shared across threads.

## What changed when records moved into a database

Records were previously one line of JavaScript Object Notation per iteration in a single
append-only file. Three things are different now, and one of them is a genuine trade rather than
an improvement.

**Experiments exist.** The old format had no notion of one. Iteration numbers simply climbed, so a
file accumulated across many sessions and the boundaries between them were not recoverable from
the data.

**Appending no longer costs more as the history grows.** Finding the next iteration number used to
require re-reading everything written so far; it is now an indexed lookup.

**Durability changed in kind.** The old format promised a *truncated but parseable* log: a crash
mid-write lost at most the final line, and a partial line was skipped on reading. A database gives
something stronger for writes that completed — each is atomic, so a half-recorded iteration cannot
be read back at all — and something weaker for the file as a whole, because a corrupted database
is not partially readable the way truncated text is. This was a deliberate exchange, not a free
upgrade.

## The imported history

The repository's pre-existing log was imported as a **single** experiment of 4,190 iterations,
named `legacy-session (pre-database)`. Its configuration records why:

> the source format recorded no run boundaries; this is one experiment only because the original
> divisions are not present in the data

That file was written across many separate sessions. Splitting it back into them is not possible
from the data, and inventing boundaries would fabricate history. Everything recorded from now on
has real boundaries.

## Naming, switching, renaming — and what a switch resets

An experiment is created by naming it, not by launching the server with a flag. The daemon starts
with none selected; the dashboard asks for a name in a dialog, and the agent is instructed to ask
the operator rather than invent one. Until then every tool that measures or records refuses with
`no_experiment`, while discovery (skills, devices, definitions) still answers.

Each experiment is a directory under the runs dir, named by a filesystem-safe form of its name,
holding `session.db`, `telemetry.json`, `flowgraphs/` and `learned_skills.jsonl`. The database
row carries the display name, the goal, and which backend measured it.

The six operations, identical from the dashboard (`POST /experiments`) and from the agent:

| Operation | Effect on the record | Effect on the session and radios |
|---|---|---|
| `current_experiment` | none | none |
| `list_saved_experiments` | none | none |
| `start_experiment(name, goal)` | new directory and row | **reset** |
| `switch_experiment(name)` | reopens; history continues | **reset** |
| `rename_experiment(name, goal?)` | renames the row; moves the directory | none |
| `finish_experiment(summary?)` | stamps `ended_at`; returns the run's outcome | none |

## Ending a run

`finish_experiment` is the only end-of-run signal in the system, and the agent calls it once, after
the last measurement and immediately before it writes its report.

Nothing can infer an ending. A ledger that has stopped growing is a finished run and an agent
pausing to read a constellation, diagnose a rung or write an annotation, and on the radios those
pauses routinely outlast any threshold worth setting. The console used to guess from quiet, and so
raised its run report in the middle of a ladder — repeatedly, because every trial that landed
afterwards re-armed the guess. An interim report is worse than none: it invites the operator to act
on a number the rest of the run will move.

So the marker is a signal, not a state. The console fires on the TRANSITION to a newly stamped
`ended_at` and on nothing else, which is why opening it on a run that finished yesterday stays
quiet. Appending anything to a concluded experiment clears the stamp, so a run that turns out to
have more to measure simply carries on and is concluded again at its true end, raising a second,
later report. Calling `finish_experiment` twice with nothing in between returns `already_finished`
and changes nothing.

The return value is the run's outcome — trial counts, the structures tried, the best graded trial,
and every decision the agent took — so the report the agent writes and the report the console draws
are built from the same numbers.

## Decisions are recorded, not only measurements

A retune, a hop plan, a power change and a band sweep each write a ledger row carrying no metrics.
They are half of what a run did and a table of trials cannot show them: without these rows a
frequency-avoidance run reads back as a handful of measurements with no account of why the channel
moved between them. Because they carry no metrics, nothing that pools error ratios picks them up.

A reset clears the built structure, the inner-loop tuning, the last measurement and the agent's
device claims (the framework's grader claim stays); drops the hop plan and the agent's retune and
power settings and returns the link to the bench's declared channel; re-applies the hidden
condition; and runs the operator hook, which the daemon binds to disarming the interferer. A new
experiment must not inherit a keyed transmitter it did not ask for. The result of every start and
switch includes a report listing exactly these steps as they happened, and both the agent's tool
result and the dashboard's confirmation show the same text.
