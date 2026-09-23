# Getting started

This page takes you from a checked-out repository to an agent running an experiment. It assumes
Python 3.10 or later and GNU Radio 3.10; the radios are optional, and everything on this page
works without them.

## Start the server

```bash
cd gr-autopilot
PYTHONPATH=. python3 -m gr_autopilot.daemon --port 8080 --runs-dir runs
```

```
autopilotd  http://127.0.0.1:8080
  MCP endpoint : http://127.0.0.1:8080/mcp   (37 tools, no auth — loopback only)
  dashboard    : http://127.0.0.1:8080/
  flowgraphs   : http://127.0.0.1:8080/flowgraphs
  experiments  : http://127.0.0.1:8080/experiments   (runs dir /…/runs)
  experiment   : NONE SELECTED -- name one on the dashboard, or the agent will ask you
  devices      : http://127.0.0.1:8080/devices
  backend      : gr-spec (executes the agent's chain)
  operator     : POST http://127.0.0.1:8080/control  (the hidden channel and the interferer)
                 UNGUARDED — no --token set, so anything that reaches this port can drive it
  log          : runs/autopilotd.log   (console at info; --log-level debug for every request)
```

Leave it running. It is the gateway for everything else, and it is meant to outlive any one
session — including several experiments in a row.

The server starts with **no experiment selected**. You do not name one on the command line: a
session usually holds more than one investigation, and the name belongs to whoever is running
it, not to a launch flag. Until one is named, every tool that measures or records refuses with
`no_experiment`, and both the dashboard and the agent will ask you for a name. Each experiment is
a directory under the runs dir, `runs/<name>/`, holding its history, its last measurement and its
exported flowgraphs. (`--experiment <name>` selects or creates one at startup if you do want that;
`--ledger runs/<name>/session.jsonl` still works and means the same thing.)

The server binds to the loopback interface only. It is laboratory equipment with no
authentication by default; do not expose it to a network. If you need a token, pass `--token`
and clients must then present it as a bearer credential.

`--token` is the **only** credential. It covers `/mcp` and the write surface alike: `POST
/control` (the hidden channel, the transmit attenuation, the interferer) and `POST /experiments`.
Loopback is exempt from it everywhere, because a process on this machine could read the token out
of the daemon's command line anyway. From another machine, type it into the field on the Operator
page or the experiment dialog; the console keeps it in memory for that browser session only.

There was briefly a second secret here — an operator password the agent never received, so that
an agent able to POST still could not switch off the interference it was being graded on adapting
to. It is gone. A bench driven over the network has to hand the agent the bearer token, and one
credential cannot separate two principals. What survives is narrower and does not depend on who
is asking: **`GET /control` does not report the hidden channel quality to anyone**, the telemetry
snapshot strips it, and every tool response is scanned for it. No tool posts to `/control`, so an
agent confined to the tool surface still cannot reach it; an agent given a shell on the bench host
can, which is why a graded run gives it the tools and nothing else.

## Reading the log

Everything the daemon does leaves one line, on the console and in a rotating file
(`runs/autopilotd.log` by default; `--log-file` moves it, `--log-file ''` turns it off). The console
shows `info` and above unless you pass `--log-level debug`; the file always records `debug`, so it
is the place to look for something that happened an hour ago.

Each line names its component: `mcp` for tool calls (name, outcome, duration, the identifying
arguments), `http` for the portal and the operator surface (every `/control` and `/experiments`
command with the caller's address, and every refusal), `service` for experiments starting,
switching and renaming with the reset report, `worker` for the radio subprocess coming and
going, `jammer` for the interferer arming — a **warning**, because it transmits — and disarming.

```
18:12:04 INFO    mcp                    tool run_flowgraph ok (1188 ms) n_bits=50000
18:12:09 INFO    http                   operator command from 10.0.0.136: {'tx_atten_db': 18.0}
18:12:20 WARNING jammer                 interferer ARMED: cw at 2370.100 MHz, if_gain 20
18:13:02 ERROR   worker                 radio worker dead (pid 4412, exit -6): ...
```

A browser on another machine dropping an idle connection used to print a full traceback each
time; it is now a `debug` line. Anything a request handler raises that is *not* a dropped
connection is logged once, at `error`, with its traceback.

## Connect a client

The Model Context Protocol (MCP) endpoint is at `/mcp`. For Claude Code:

```bash
claude mcp add --transport http gr-autopilot http://127.0.0.1:8080/mcp
```

Confirm it connected:

```bash
claude mcp list
# gr-autopilot: http://127.0.0.1:8080/mcp (HTTP) - ✔ Connected
```

Note that this is an HTTP transport pointing at a *running* server, not a command the client
launches. The reasoning is in [system architecture](/wiki/architecture); the practical
consequence is that the server must already be running, and that the dashboard and the agent
observe the same experiment.

!!! note "The other transport"
    A standard-input/output entry point (`python3 -m gr_autopilot.mcp_server`) still exists and is
    still supported. Under it the client launches the server as a child process, which means one
    client, no dashboard, and no operator channel. It is convenient for a quick session; the
    daemon is the real thing.

## The portal, and reaching it from another machine

The page at `http://127.0.0.1:8080/` has a left-hand navigation with one page per endpoint the
daemon serves: **Overview** (`/data` — measurement tiles, constellation, spectrum, waterfall and trend plots, with the edit ledger in a resizable, collapsible rail on the right), **Devices** (`/devices`),
**Topology** (`/topology`, drawn as a graph with the path-check measurements on its edges),
**Experiments** (`/experiments`), **Flowgraphs** (`/flowgraphs`, listed, downloadable and drawn),
**Prompts** (`/prompts`), **Tools** (the agent's surface, read over `/mcp`), **Operator**
(`/control`: the hidden channel and the interferer, behind the same `--token`) and **Docs**
(`/wiki`). Every page links to the JSON it renders.

The daemon binds to loopback by default, on purpose: `/mcp` can key a transmitter and `/control`
can move the interferer. Two ways to use the portal from a second computer:

1. **An ssh tunnel — recommended.** Nothing changes on the bench host. On the other computer:

   ```bash
   ssh -N -L 8080:127.0.0.1:8080 <user>@<bench-host>     # e.g. operator@10.0.0.222
   ```

   then open `http://localhost:8080/`. Everything, including the operator page, behaves exactly
   as it does on the bench host, and nothing is listening on the network.

2. **Bind to the network — with a token.** The daemon refuses a non-loopback bind without one:

   ```bash
   PYTHONPATH=. /usr/bin/python3 -m gr_autopilot.daemon --host 0.0.0.0 --port 8080 \
       --token $(openssl rand -hex 16) ...
   ```

   then open `http://<bench-host>:8080/` — by name where mDNS is running (`http://cradle.local:8080/`
   on this bench) or by address. The dashboard's read-only pages need nothing further. Naming,
   switching or renaming an experiment needs the same bearer token (the dialog asks for it when
   the page is not on the bench host), and so does the Operator page. The
   Tools page reads `/mcp`, which accepts browsers only from the bench host itself, and says so. Clients on the bench host are
   exempt from the bearer token (the machine boundary is their auth), so the local `claude mcp`
   entry keeps working unchanged. An MCP client on any *other* machine must send it:

   ```bash
   claude mcp add --transport http gr-autopilot http://10.0.0.222:8080/mcp \
       --header "Authorization: Bearer <token>"
   ```

   Do this only on a network you control: the token is the only thing between the network and a
   transmitter.

## Run your first experiment

Open <http://127.0.0.1:8080/> — with nothing selected the page opens a dialog asking what to
call the experiment. Name it there, or ask the connected model, which has been told to ask *you*
for a name before it measures anything, and to relay what a switch resets:

> Build a QPSK link and show me that it works.

(The model will first ask something like *"What should I call this experiment?"* — answer, and it
calls `start_experiment`. The name becomes the directory everything is written to. The library's
*Name the experiment before you measure* exercise is exactly this step, spelled out.)

Or pick one of the ready-made exercises from [the experiment library](/wiki/library) — a client
that supports MCP prompts will offer them as a menu. The library covers the textbook cases:
building a link at a chosen bandwidth and centre frequency, measuring what a matched filter is
worth, watching a receiver fail without timing recovery, finding interference, out-running a
jammer.

Open <http://127.0.0.1:8080/> while it runs. The dashboard shows the current measurement, the
recovered constellation, and the iteration history as they happen.

## Using the radios

```bash
PYTHONPATH=. python3 -m gr_autopilot.daemon --port 8080 --radios \
    --tx-uri ip:192.168.2.1 --rx-uri ip:192.168.3.1
```

The server starts a separate worker process that owns the radios and confirms both answer before
reporting ready. If a radio is absent the server still starts — it simply reports the devices as
unavailable, because the server is the gateway and must come up regardless.

Check what it found:

```bash
curl -s http://127.0.0.1:8080/devices
```

```json
{"devices": {"tx": {"uri": "ip:192.168.2.1", "alive": true, "temp_c": 37.7},
             "rx": {"uri": "ip:192.168.3.1", "alive": true, "temp_c": 36.8}},
 "backend": "pluto",
 "worker": {"running": true, "pid": 183136, "reason": "ready"}}
```

!!! warning "Transmitting"
    Running with `--radios` keys a real transmitter. Use a cabled path with appropriate
    attenuation, or a **shielded** enclosure — note that *anechoic* is not *shielded*: absorber
    suppresses reflections and does nothing to contain emission. The jammer will not arm at all
    unless the bench declaration attests to containment (`attestation.medium` plus
    `antennas_attached: false` for coax, or `shielded: true` for a chamber); see
    [devices](/wiki/devices).

!!! danger "Start the daemon with an interpreter that has GNU Radio"
    The radio worker inherits `sys.executable` from the daemon. Launching as bare `python3` under
    an environment that lacks GNU Radio produces a worker that answers health checks, reports
    `ready`, and then fails on the first measurement. The worker refuses at startup with a message
    naming the interpreter, but the fix is to use the right one — on a Debian/Ubuntu host with a
    system GNU Radio, that is `/usr/bin/python3`, not a conda python earlier on `PATH`.

## Take the flowgraph away

Ask the agent to export what it built, or call the tool directly. The result is a GNU Radio
Companion file:

```bash
curl -s http://127.0.0.1:8080/flowgraphs
# {"flowgraphs": ["qpsk_link.grc"], "dir": "...", "open_with": "gnuradio-companion"}

curl -s http://127.0.0.1:8080/flowgraphs/qpsk_link.grc -o mine.grc
gnuradio-companion mine.grc
```

On the `gr-spec` backend every block in that file is a block the experiment actually ran (the
Pluto backend runs a fixed receiver and the file then records the *requested* chain — see
[signal-processing chains](/wiki/flowgraphs)). Edit it — change the excess bandwidth, widen a
loop, add carrier recovery — save it, and hand it back:

> Load the flowgraph at /path/to/mine.grc and measure it again.

The system then runs **your** graph, and records the iteration as hand-edited with the file's
content hash, so a result from an edited graph stays distinguishable from one the agent proposed.
[Signal-processing chains](/wiki/flowgraphs) explains what survives the round trip and what does
not.

## Where things are kept

| What | Where |
|---|---|
| Experiments and iterations | `runs/<name>/session.db` (SQLite; one experiment per directory) |
| Latest measurement snapshot (what the dashboard draws) | `runs/<name>/telemetry.json`, written after every graded trial |
| Exported flowgraphs | `runs/<name>/flowgraphs/` |
| The agent's promoted vocabulary | `runs/<name>/learned_skills.jsonl` |
| Dashboard | served from `dashboard/dist` |
| This documentation | `docs/wiki/*.md`, rendered on request |

`<name>` is a filesystem-safe form of the name you gave ("Ladder @ 2370 MHz" becomes
`ladder-2370-mhz`); the display name, the goal and which backend measured it are recorded in the
database and shown in the dashboard header. Change the runs dir with `--runs-dir`.

### Switching, renaming, starting another

From the dashboard: click the experiment chip in the header (or **New experiment** on the
Experiments page). From the agent: `list_saved_experiments`, `switch_experiment(name)`,
`start_experiment(name, goal)`, `rename_experiment(name)`. Both go through the same code.

**Starting or switching resets the session and the radios**, and says so before you confirm and
again afterwards, listing what was cleared: the built structure, tuning and the agent's device
claims; the hop plan and any retune (the link returns to the bench's declared channel); and the
interferer, which is disarmed if it was transmitting. Each experiment's history is kept — switch
back and it continues where it stopped. **Renaming resets nothing**: it moves the directory and
keeps the built link.

## Running the tests

```bash
PYTHONPATH=. python3 -m pytest -q
```

The suite needs no radios: hardware paths are exercised against a simulated backend running in a
real worker subprocess, so process supervision, device loss and recovery are all covered without
anything being plugged in.
