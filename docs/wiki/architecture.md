# System architecture

The system is one long-lived server that never touches a radio, and a short-lived worker process
that does nothing else. Almost every other design decision follows from that split, so this page
explains the reason for it before describing the parts.

## Why the radios are held at arm's length

A radio here is reached over a network protocol by the `libiio` library, wrapped by GNU Radio.
When such a device stops responding, the behaviour depends on *how* it stopped, and the two cases
are not alike. Both were measured on the bench.

| How the connection ends | What happens |
|---|---|
| Closed cleanly | A warning is logged, the source stops producing, **the process survives** |
| Goes silent | The library throws from one of its own threads; nothing catches it; the C++ runtime terminates the process with `SIGABRT` |

The second case is the one that matters, because **an unplugged cable produces silence, not a
clean close**: the network interface disappears and packets simply stop. No Python exception
handler can intervene — the process is killed outright, with a core dump.

There is a related nuisance: these device contexts can hang on destruction, which is why the
project's hardware scripts historically ended by exiting immediately rather than shutting down
politely.

A server that must stay up therefore cannot hold a radio. Isolating the radio in its own process
turns both problems into ordinary events: that process dying **is** the signal that the device is
gone, and its abrupt exit is normal rather than something to prevent.

## The parts

### The daemon

`autopilotd` is the long-lived process. It listens on the loopback interface and serves, from one
port:

| Path | Purpose |
|---|---|
| `/mcp` | The Model Context Protocol endpoint an agent connects to |
| `/` | The live dashboard |
| `/data` | The dashboard's polling endpoint: latest measurement, recent iterations, and which experiment they belong to (`null` until one is named) |
| `/devices` | Per-radio liveness, measured when asked |
| `/experiments` | The experiment records |
| `/flowgraphs` | Exported GNU Radio Companion files |
| `/prompts` | The experiment library |
| `/wiki` | This documentation |

It owns the database, the device registry and exactly one service object. It never imports the
radio libraries.

### The radio worker

One child process per hardware session, owning exactly one device context. It reads a request per
line and writes a reply per line over a pipe: run a link, sense the spectrum, set the transmit
condition, report device health, exit.

It has no network port and no knowledge of the dashboard or of any model. Measurements cross the
pipe as raw buffers rather than as lists of decimal numbers, because a trial produces tens of
thousands of values and encoding those as text would cost more than the measurement.

Before reporting ready it confirms both radios answer. Constructing the backend alone is not
enough: it builds lazily, so a backend created against an absent radio succeeds and the failure
would not surface until the first measurement — by which time an experiment is under way waiting
for a result that will never arrive.

On the simulated backend there is **no child process at all**. Simulation runs in the daemon,
so the common case stays simple and fast.

### The heartbeat

Device liveness is one attribute read per radio, over a separate short-lived connection, costing
a few milliseconds. Two alternatives look reasonable and are not; both were measured.

- **Capturing samples over a second connection** appears to work, because the device server
  multiplexes it — while silently taking two whole buffers away from the capture already in
  progress. A health check that corrupts the measurement it is watching is worse than none.
- **The repository's stand-alone health script writes** tuning frequency, gain mode, sample rate
  and gain. Run periodically it would overwrite whatever the experiment had just configured, and
  the resulting bad measurement would look like physics rather than like a probe.

While the worker is alive it answers health queries, because it is the process holding the
radios. Once it has died nobody holds them, so each device is probed directly from the daemon.
That distinction matters: losing the receiver kills the worker, but the transmitter is usually
still perfectly healthy, and reporting both as gone would blank a working device and misdescribe
the fault.

## How an agent talks to it

The protocol is spoken over two transports that share one implementation. The protocol logic
takes a decoded message and returns a reply, knowing nothing about pipes or sockets; each
transport is a thin wrapper around it.

**Standard input and output.** The client launches the server as a child process and speaks over
its pipes. Simple, and appropriate for a single session.

**Streamable HTTP.** The server listens; clients send requests to a URL. This is what the daemon
offers, and the reasons are practical rather than aesthetic:

- The server outlives any one session, so the radios are not re-acquired per conversation.
- The dashboard and the agent observe the **same** experiment, because there is only one.
- There is a second channel. Under the standard-input transport the client owns the process, so
  there is nowhere for a human operator to set the hidden channel condition without the agent
  seeing it. A listening socket makes an operator surface possible.

The HTTP transport answers a request with a single reply. It does not offer the server-initiated
event stream the protocol also allows; a `GET` on the endpoint returns 405, which the
specification permits. The practical consequence is that a long tool call blocks until it
finishes.

## The record

Experiments and their iterations live in a SQLite database — part of the Python standard library,
so no dependency, and a single file that can be copied or deleted like any other. The model is
two levels deep: an experiment contains iterations, and may have artifacts such as an exported
flowgraph. [Experiments and their records](/wiki/experiments) describes it in full.

Write-ahead logging is enabled so the dashboard polling for state never blocks the measurement
loop appending to it. Connections are created per thread, because this interpreter's SQLite
module reports a thread-safety level at which one connection must not be shared between threads.

## Logging

One configuration (`gr_autopilot/logsetup.py`) serves the daemon and everything it hosts: a console
handler at the level given by `--log-level` (default `info`), and a rotating file — default
`<runs-dir>/autopilotd.log`, five megabytes by five — that always records `debug`. Loggers are
named by component so a line can be read without context: `mcp` (every tool call: outcome and
duration at `info`, the full arguments at `debug`, an internal exception at `error` with its
traceback rather than silently converted into a tool error), `http` (operator commands and
experiment changes at `info` with the caller's address, refusals at `warning`, the access line
at `debug`), `service` (experiment lifecycle and what a reset cleared), `worker` (the radio
subprocess starting, stopping and dying) and `jammer` (arming at `warning`, since it transmits).

The standard-library HTTP server reports every exception a handler thread raises as a traceback
on standard error, and a browser on another machine closing an idle keep-alive connection is such
an exception — so a portal viewed over the network used to fill the console with them. The
daemon's server treats a dropped connection as `debug` and reserves the traceback for a handler
that actually failed. Idle connections are also reaped after two minutes rather than held open.

## What the dashboard shows, and what it refuses to

The dashboard polls `/data` and `/devices`. Its guiding rule is that it must show the **current**
state of the system rather than the last state it happened to see.

The measurement snapshot is a last-value file that outlives the run which wrote it, so every
snapshot carries a timestamp and the server reports its age. The page shows one of three states:
**live** when a measurement arrived within the staleness window (6 s), **idle** when the server is
answering but nothing is running, and **offline** when it is not answering at all. Only the live
state reads as current. When idle or offline the last frame is *kept* — dimmed, desaturated, and
headed by a banner stating its age — rather than removed: blanking would discard the record of the
run that just finished at exactly the moment an operator wants to read it, and a labelled old frame
cannot be mistaken for a live one. The edit ledger sits in its own full-height rail on the right,
independent of this switch, so the history is visible in every state including before any run.
The header carries the experiment name and its provenance (radios or a model) at all times.

The same rule applies per radio. If the receiver is lost, the constellation and spectrum are
replaced with a statement that the receiver is unavailable — both plots are made from what the
receiver captured — while the transmitter tile stays green.
