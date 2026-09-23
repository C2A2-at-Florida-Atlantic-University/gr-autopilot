"""``autopilotd`` -- the gr-autopilot daemon: one long-lived local server.

Start it once and leave it running. It listens on the loopback interface and serves, from a
single process and a single port:

  ``/mcp``    the Model Context Protocol (MCP) endpoint the agent's client connects to, over
              Streamable HTTP (see ``tools/mcp_http.py``)
  ``/``       the telemetry dashboard (the pre-built web application in ``dashboard/dist``)
  ``/data``   the dashboard's polling endpoint (latest measurement snapshot + iteration history)

Why a daemon rather than the stdio server. Under the stdio transport the CLIENT launches the
server as a child process, so the server lives and dies with one chat session and nothing else can
reach it. A laboratory instrument needs the opposite: a process that outlives any one session, so
that the dashboard and the agent observe the SAME experiment, and so that a human operator has a
control channel the agent cannot see. Both follow from listening on a socket.

The stdio entry point (``python -m gr_autopilot.mcp_server``) is unchanged and still supported.

Hardware runs behind a worker subprocess (``--radios``), never in this process: a lost radio
does not fail gracefully -- a silently disconnected device makes the GNU Radio input/output
library abort the hosting process (measured: SIGABRT with a core dump). Owning a radio here would
let an unplugged cable kill the daemon, which is precisely what the daemon exists to prevent.
Without ``--radios`` the simulated backend runs and the device tools report manifests marked
``simulated``.

Run:
    python3 -m gr_autopilot.daemon
    python3 -m gr_autopilot.daemon --port 8080 --token $(openssl rand -hex 16)

Connect an MCP client (note: HTTP transport, not a spawned command):
    claude mcp add --transport http gr-autopilot http://127.0.0.1:8080/mcp
"""
from __future__ import annotations

import argparse
import atexit
import logging
import signal
import sys
from pathlib import Path

from gr_autopilot.telemetry.server import ControlState, serve
from gr_autopilot.tools import AutopilotService, StdioMCPServer
from gr_autopilot.tools.mcp_http import MCPHttpEndpoint


def default_sim_backend():
    """The simulated backend the daemon uses when no radios are attached.

    This is the one that EXECUTES the agent's chain as a real GNU Radio flowgraph, so that
    structural choices -- and edits an operator makes to an exported graph -- change what is
    measured. The pure-numpy backend is the reference against which error-ratio theory is
    checked, but it ignores pulse shaping and synchronization entirely, so running the daemon on
    it would make every chain measure the same and quietly undo the point of the flowgraph being
    real. It remains the fallback when GNU Radio is not installed.
    """
    try:
        from gr_autopilot.link.gr_spec import GRSpecBackend
        import gnuradio  # noqa: F401  - confirm the toolkit is actually importable
        return GRSpecBackend(), "gr-spec (executes the agent's chain)"
    except Exception:  # noqa: BLE001
        from gr_autopilot.link.numpy_sim import NumpySimBackend
        return NumpySimBackend(), "numpy-sim (GNU Radio unavailable; chains are not executed)"


def attach_telemetry(server, service, snapshot_path=None, goal: str = ""):
    """Write a dashboard snapshot after every graded trial.

    The dashboard renders a snapshot FILE, and nothing on the tool surface writes one. Until this
    hook existed the daemon served ``/data`` with ``"snapshot": null`` forever, and the page
    correctly turned that into IDLE -- so a session driven entirely over ``/mcp`` displayed "no
    MCP activity; nothing is running" while it was measuring on real radios. The only writers were
    the drivers in ``scripts/``, which built their own; a client that connected to the daemon got
    a live experiment and a dead console.

    Fired through ``StdioMCPServer.on_run_flowgraph``, which exists for exactly this and swallows
    whatever the callback raises: telemetry must never be able to break a measurement.
    """
    from gr_autopilot.telemetry import TelemetryWriter

    # One writer per snapshot path. The path follows the selected experiment, so it is resolved
    # on every trial rather than fixed when the hook was installed; an explicit ``snapshot_path``
    # (the old contract) pins it.
    writers: dict = {}

    def writer_for():
        path = snapshot_path or service.snapshot_path
        if path is None:
            return None
        key = str(path)
        if key not in writers:
            writers[key] = TelemetryWriter(path)
        return writers[key]

    def emit(srv):
        writer = writer_for()
        if writer is None:
            return  # no experiment selected: nothing measured, nothing to draw
        result, metrics = service._last_result, service._last_metrics
        spec = service._spec
        # An aborted trial (a radio went away) clears these deliberately -- it is the ABSENCE of a
        # measurement. Drawing the previous one under a new timestamp would age it as fresh.
        if result is None or metrics is None or spec is None:
            return
        modcod = spec.modulation + (f"+{spec.coding}" if spec.coding else "")
        # The backend reports what it actually ran at; the agent's requested centre frequency is
        # only a request until a radio honours it, so prefer the measured value and fall back.
        meta = result.meta or {}
        center_hz = meta.get("center_freq_hz") or service._agent_rf.get("center_freq_hz")
        # Span of the spectrum axis. The transform is fed recovered symbols (one sample per
        # symbol), so the axis spans the SYMBOL rate; sample_rate/sps reconstructs it for a
        # backend that reports only the sample rate.
        sps = meta.get("sps") or (spec.pulse_shape or {}).get("sps") or 1
        span_hz = meta.get("symbol_rate") or (
            (meta.get("sample_rate") / sps) if meta.get("sample_rate") else None)
        writer.write(
            result=result,
            metrics=metrics,
            target_ber=service._last_author_target,
            span_hz=span_hz,
            center_hz=center_hz,
            structure={"modcod": modcod,
                       "center_freq_hz": center_hz,
                       "symbol_rate_hz": span_hz},
            active_loop={"loop": "outer",
                         "detail": f"run {spec.structure_id} via MCP tool call"},
            goal=goal or ((service.current_experiment() or {}).get("goal") or ""),
            tokens=srv.token_stats(),
        )

    server.on_run_flowgraph = emit
    return writer_for


def build(snapshot_path=None, ledger_path=None, es_n0_db: float = 12.0,
          token: str | None = None, log_path: str | None = None, backend=None,
          device_backend: str = "mock", device_uris: dict | None = None,
          topology=None, topology_findings=None,
          experiment: str | None = "session", goal: str = "", experiment_backend: str = "",
          runs_dir=None):
    """Wire the service, the MCP protocol handler, and the HTTP transport together.

    ``device_backend``/``device_uris`` describe the radios the DEVICE tools report, and must be
    passed whenever the link backend is real: the two travelled separately once, and the daemon
    served mock manifests -- wrong chip, wrong tuning range, wrong clock -- while transmitting on
    hardware, leaving the agent to reason about radios that were not on the bench.

    Returns ``(endpoint, service, server)`` so tests can drive the transport without binding a
    socket.
    """
    channel = {} if (backend is not None and backend.owns_channel) else {"es_n0_db": es_n0_db}
    service = AutopilotService(backend=backend, channel=channel,
                               ledger_path=Path(ledger_path) if ledger_path else None,
                               device_backend=device_backend, device_uris=device_uris,
                               topology=topology, topology_findings=topology_findings,
                               experiment=experiment, experiment_goal=goal,
                               experiment_backend=experiment_backend, runs_dir=runs_dir)
    server = StdioMCPServer(service, log_path=log_path)
    # Telemetry is always wired: the snapshot lands in the selected experiment's directory (or
    # at an explicit --snapshot path); an explicit path must never be silently dropped, or the
    # dashboard stays IDLE whatever the operator passed.
    attach_telemetry(server, service, snapshot_path or None, goal=goal)
    return MCPHttpEndpoint(server, token=token), service, server


def build_radio_backend(args):
    """Start a radio worker and wrap it as a link backend.

    The daemon never imports the radio libraries itself. The worker process does, and if a radio
    disappears that process is killed outright by the input/output library (see
    :mod:`gr_autopilot.hardware.radio_worker`). Keeping it at arm's length is what lets this
    server stay up while a device comes and goes.
    """
    from gr_autopilot.hardware.supervisor import RadioWorker, WorkerBackend
    from gr_autopilot.link.hopping_backend import HoppingBackend

    worker = RadioWorker(backend="pluto", tx_uri=args.tx_uri, rx_uri=args.rx_uri,
                         sample_rate=args.sample_rate, rx_gain_db=args.rx_gain,
                         tx_atten_db=args.tx_atten,
                         pilot_spacing=args.pilot_spacing)
    # A radio cannot average a hop plan into its noise floor the way the sim backend does -- it has
    # to actually retune every dwell. HoppingBackend is that motion, and it is a TRANSPARENT
    # pass-through until a hop plan is set, so Experiments 1 and 2 (which never set one) run the
    # worker backend exactly as before. Without this wrapper set_hop_plan reached no radio at all:
    # the plan was stringified over the worker protocol and dropped as an unknown condition key,
    # the link stayed on one channel, and only the following jammer moved.
    return HoppingBackend(WorkerBackend(worker)), worker


class OperatorControl(ControlState):
    """``POST /control`` bound to the jammer instrument and the hidden channel condition.

    The base class only remembers operator commands for a polling loop to read. The daemon has no
    such loop -- an external agent drives it over the tool protocol -- so the command must take
    effect as it arrives. A refusal from the instrument propagates out as an error rather than
    being recorded as state, so an operator is never told a jammer is on when it is not.

    Both things it drives are on the operator's side of the integrity split: the jammer, and how
    hard the link is. Neither is reachable from the agent's tool surface, which is the whole
    reason a graded result means anything.
    """

    #: Hidden-condition knobs. Named here rather than inferred so that adding an agent-visible
    #: control field later cannot silently become an operator one.
    CHANNEL_KEYS = ("tx_atten_db", "rx_gain_db")

    def __init__(self, jammer, service=None, **initial):
        super().__init__(**initial)
        self._jammer = jammer
        self._service = service

    def apply(self, cmd: dict) -> dict:
        touches_jammer = any(k == "jammer" or k.startswith("jammer_") for k in (cmd or {}))
        state = super().apply(cmd)
        if touches_jammer:
            state["jammer_status"] = self._jammer.apply(cmd)
            state["jammer"] = state["jammer_status"]["armed"]
        # Merged, not replaced: a campaign steps one knob at a time and must not silently drop
        # the rest of the condition. Validated values come back from the base class.
        changes = {k: state[k] for k in self.CHANNEL_KEYS if k in (cmd or {}) and k in state}
        if changes and self._service is not None:
            state["channel"] = self._service.update_channel(**changes)
        return state

    def get(self) -> dict:
        return {**super().get(), "jammer_status": self._jammer.status()}


def _scrub_argv(argv) -> str:
    """The command line for the log, with every secret removed: both the ``--token VALUE`` and
    the ``--token=VALUE`` spelling."""
    secret_flags = ("--token",)
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            out.append("…")
            continue
        if a in secret_flags:
            out.append(a)
            skip = True
        elif a.startswith(tuple(f + "=" for f in secret_flags)):
            out.append(a.split("=", 1)[0] + "=…")
        else:
            out.append(a)
    return " ".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to bind (default loopback; do NOT expose this to a network)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--es-n0-db", type=float, default=12.0,
                    help="hidden simulated channel quality the agent must discover by measurement")
    ap.add_argument("--token", default="",
                    help="require 'Authorization: Bearer <token>' on /mcp (default: no auth, "
                         "acceptable only on loopback)")
    ap.add_argument("--runs-dir", default="runs",
                    help="root under which every experiment gets its own directory "
                         "(runs/<name>/session.db, telemetry.json, flowgraphs/)")
    ap.add_argument("--ledger", default="",
                    help="(compatibility) open this experiment directory at startup, e.g. "
                         "runs/<name>/session.jsonl; prefer --runs-dir and naming the experiment "
                         "from the dashboard or the agent")
    ap.add_argument("--snapshot", default="",
                    help="telemetry snapshot file the dashboard reads (default: telemetry.json "
                         "beside the ledger, so the console works without being asked)")
    ap.add_argument("--experiment", default="",
                    help="select (or create) this experiment at startup. Default: none -- the "
                         "dashboard asks for a name, and the agent must ask the operator")
    ap.add_argument("--goal", default="", help="one line describing what this run is for")
    ap.add_argument("--log", default="", help="append the JSON-RPC transcript here")
    ap.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"],
                    help="console verbosity (the log file always records debug)")
    ap.add_argument("--log-file", default=None,
                    help="rotating log file (default: <runs-dir>/autopilotd.log; pass '' for none)")
    ap.add_argument("--radios", action="store_true",
                    help="drive real radios through a worker subprocess instead of the simulator")
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--sample-rate", type=float, default=2_084_000.0)
    ap.add_argument("--rx-gain", type=float, default=None,
                    help="receiver gain (dB). Default: the value measured on this bench and "
                         "recorded under common.calibration in the topology, else 40")
    ap.add_argument("--tx-atten", type=float, default=None,
                    help="transmit attenuation (dB). Default: as recorded in the topology, else 10")
    ap.add_argument("--topology", default="config/bench.yaml",
                    help="bench declaration to check the hardware against (default "
                         "config/bench.yaml; pass '' to run with no declaration, in which case "
                         "device identity is unverifiable)")
    ap.add_argument("--pilot-spacing", type=int, default=None,
                    help="PSAM pilot every N payload symbols (0 = off). Resolves the "
                         "90-degree carrier ambiguity that breaks coded 16-QAM. "
                         "Default: common.calibration.pilot_spacing in the topology")
    ap.add_argument("--verify-path", action="store_true",
                    help="tier-B check at startup: TRANSMIT briefly to confirm the transmitter "
                         "actually reaches the receiver (and the jammer too, if declared). Off by "
                         "default because it keys a transmitter; identity checks need no such flag")
    ap.add_argument("--jammer", action="store_true",
                    help="allow the operator to key a HackRF jammer on the cabled path. RESEARCH "
                         "USE ONLY: this transmits. Off by default; see RESPONSIBLE-USE.md")
    args = ap.parse_args(argv)
    from gr_autopilot.logsetup import configure as _configure_logging
    # None = flag absent -> the default beside the runs dir; "" = explicitly no file.
    log_file = str(Path(args.runs_dir) / "autopilotd.log") if args.log_file is None else args.log_file
    log_path = _configure_logging(args.log_level, log_file or None)
    log = logging.getLogger("gr_autopilot.daemon")
    log.info("autopilotd starting: %s", _scrub_argv(argv if argv is not None else sys.argv[1:]))
    # The MCP surface can key a real transmitter and the operator channel can move the
    # interferer. On loopback the machine boundary is the auth; on any other interface it
    # is not, so a network bind without a bearer token is refused rather than warned about.
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not args.token:
        ap.error(f"--host {args.host} exposes /mcp beyond this machine; add --token "
                 "$(openssl rand -hex 16) (clients elsewhere send it as 'Authorization: "
                 "Bearer ...'; clients on this machine are exempt), "
                 "or keep the default loopback bind and reach the portal over an ssh tunnel.")

    # The declared bench, loaded FIRST: it carries the measured gain settings the worker starts
    # with, so it has to be read before the radios are opened. A schema or cardinality error is
    # FATAL -- a bench that cannot be described is not one to run experiments on, and starting
    # anyway defers the problem to whoever reads the results. A missing default file is not an
    # error, only an explicitly named one.
    topology, topo_findings, topo_label = None, None, "none declared (identity unverifiable)"
    if args.topology:
        from gr_autopilot.hardware.topology import TopologyError, load_topology
        explicit = any(a.startswith("--topology") for a in (argv if argv is not None else sys.argv))
        if Path(args.topology).exists() or explicit:
            try:
                topology = load_topology(args.topology)
            except TopologyError as exc:
                log.error("topology: %s", exc)
                return 2
            topo_label = f"{topology.name} ({args.topology}, sha {topology.sha256[:12]})"

    # Gains the operator MEASURED on this bench beat the built-in defaults, which were calibrated
    # for a path that may no longer exist. A command-line value still wins over both.
    cal = (topology.common.get("calibration") or {}) if topology is not None else {}
    gain_label = "built-in defaults"
    if args.rx_gain is None:
        args.rx_gain = float(cal.get("rx_gain_db", 40.0))
    if args.tx_atten is None:
        args.tx_atten = float(cal.get("tx_atten_db", 10.0))
    if args.pilot_spacing is None:
        args.pilot_spacing = int(cal.get("pilot_spacing", 0))
    if cal:
        gain_label = f"from {args.topology}"

    backend, worker, backend_label = (None, None, "")
    if args.radios:
        from gr_autopilot.hardware.supervisor import DeviceUnavailable
        try:
            backend, worker = build_radio_backend(args)
        except DeviceUnavailable as exc:
            # Not fatal: the server is the gateway and must come up regardless. The radios are
            # simply reported as unavailable until they are plugged back in.
            log.warning("radios unavailable at startup: %s", exc)
            backend, worker = None, None
    if backend is None:
        backend, backend_label = default_sim_backend()

    # The device tools describe the radios the worker actually holds. If that read fails the
    # server still comes up (it is the gateway), but it falls back to manifests marked
    # ``simulated`` and says so here rather than presenting them as the bench.
    # The snapshot follows the selected experiment unless the operator pins it.
    snapshot = args.snapshot or None
    # Which experiment to open at startup. None means NONE: the dashboard asks for a name and the
    # agent has to ask the operator. --ledger (compatibility) opens that directory directly.
    experiment = args.experiment or None
    # Provenance, recorded WITH the numbers: whether they came from radios or from a model is not
    # recoverable from a BER afterwards, and it is the first thing a reader needs to know.
    exp_backend = (f"pluto radios ({args.tx_uri} tx, {args.rx_uri} rx)"
                   if worker is not None else (backend_label or "simulated"))
    common = dict(snapshot_path=snapshot, ledger_path=args.ledger or None,
                  es_n0_db=args.es_n0_db, token=args.token or None,
                  log_path=args.log or None, backend=backend, topology=topology,
                  experiment=experiment, goal=args.goal, experiment_backend=exp_backend,
                  runs_dir=args.runs_dir)
    device_label = "simulated (no radios attached)"
    if worker is not None:
        try:
            endpoint, service, _server = build(
                **common, device_backend="pluto",
                device_uris={"tx": args.tx_uri, "rx": args.rx_uri})
            device_label = f"read from the radios ({args.tx_uri} tx, {args.rx_uri} rx)"
        except Exception as exc:  # noqa: BLE001 - any probe failure must not stop the gateway
            log.warning("device detection failed (%s); device tools will report SIMULATED "
                        "manifests while the link runs on real radios", exc)
            endpoint, service, _server = build(**common)
            device_label = "SIMULATED — detection failed, see warning above"
    else:
        endpoint, service, _server = build(**common)

    # Tier A: does the hardware agree with the declaration? Only meaningful against real
    # manifests -- simulated devices do not contradict a declaration, they simply do not test it,
    # and treating that as a fault would make every no-radio run refuse to measure.
    if topology is not None:
        real = [d for d in service.devices.values() if not d.simulated]
        if real:
            from gr_autopilot.hardware.topology import verify_identity
            topo_findings = verify_identity(topology, {
                "transmitter": service.devices[service.tx_device],
                "receiver": service.devices[service.rx_device]})
            service.topology_findings = topo_findings
            if topo_findings:
                log.error("topology FAULT — the hardware disagrees with %s: %s. Experiments are "
                          "refused until this is resolved.", args.topology, "; ".join(topo_findings))
                topo_label += "  ** FAULT **"
            else:
                topo_label += "  verified against the radios"
        else:
            topo_label += "  (devices simulated — declaration not tested)"

    # The jammer is framework/operator property: it is deliberately absent from the agent's
    # device inventory and tool surface, and appears only here and on /control. An agent told
    # where the interference is has not adapted to anything.
    #
    # Arming is gated on the topology's attestation. No measurement distinguishes a cable from an
    # antenna, so a human has to say so, in a file, with a date on it — and that statement is the
    # only thing standing between a misconfigured bench and an illegal transmission.
    jammer_enabled, jammer_why = bool(args.jammer), ""
    if args.jammer:
        if topology is None:
            jammer_enabled, jammer_why = False, (
                "no topology declared, so no attestation that the path is closed")
        else:
            ok, why = topology.jammer_permitted()
            jammer_enabled, jammer_why = ok, why
        if not jammer_enabled:
            log.warning("--jammer refused: %s", jammer_why)
    from gr_autopilot.hardware.jammer import JammerInstrument
    jammer = JammerInstrument(
        enabled=jammer_enabled, sample_rate=args.sample_rate,
        link_freq=service.current_link_freq)
    control = OperatorControl(jammer, service=service)

    # Starting or switching an experiment must not inherit a keyed interferer. This is the one
    # place the agent's tool surface can reach the jammer, and only in the safe direction (off).
    def _on_reset(reason: str) -> list:
        if not jammer.is_armed():
            return ["interferer was not armed"]
        control.apply({"jammer": False})
        return ["interferer DISARMED (it was transmitting)"]
    service.on_reset = _on_reset

    # A process holding a keyed transmitter must not be able to exit and leave it transmitting.
    # PR_SET_PDEATHSIG in the interferer covers a hard parent crash; these cover an orderly one.
    atexit.register(jammer.shutdown)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        _prev = signal.getsignal(_sig)

        def _stop(signum, frame, _prev=_prev):
            jammer.shutdown()
            if callable(_prev):
                _prev(signum, frame)
            else:
                raise SystemExit(0)
        signal.signal(_sig, _stop)

    # Mutable holder: the path check runs after this closure is defined (it transmits, so it goes
    # last, once the operator can see which radios are about to be keyed), but /devices must report
    # whatever the latest check found.
    path_check = {"result": None}

    def device_report():
        report = {"jammer": jammer.status(),
                  "path": path_check["result"].to_dict() if path_check["result"] else None}
        if worker is None:
            return {**report, "devices": {}, "backend": "simulated",
                    "note": "no radios are attached to this server"}
        status = worker.status()
        return {**report, "devices": worker.poll_devices(), "backend": "pluto",
                "worker": {"running": status.running, "pid": status.pid,
                           "reason": status.reason, "signal": status.as_dict()["signal"]}}

    url = f"http://{args.host}:{args.port}"
    print(f"autopilotd  {url}")
    print(f"  MCP endpoint : {url}/mcp   ({len(_server.tools)} tools, "
          f"{'token required off-loopback; this machine exempt' if args.token else 'no auth — loopback only'})")
    print(f"  dashboard    : {url}/")
    print(f"  flowgraphs   : {url}/flowgraphs   (exported .grc, open with gnuradio-companion)")
    print(f"  experiments  : {url}/experiments   (runs dir {Path(args.runs_dir).resolve()})")
    cur = service.current_experiment()
    if cur.get("selected"):
        print(f"  experiment   : {cur['name']}  ({cur['dir']})")
    else:
        print(f"  experiment   : NONE SELECTED -- name one on the dashboard, or the agent will ask "
              f"you; measuring tools refuse until then")
    print(f"  devices      : {url}/devices")
    print(f"  manifests    : {device_label}")
    if worker is not None:
        print(f"  backend      : radios via worker process (pid {worker.status().pid}); "
              f"losing a radio kills that worker, not this server")
    else:
        print(f"  backend      : {backend_label}"
              f"{' — radios requested but unavailable' if args.radios else ''}")
    print(f"  topology     : {topo_label}")
    if topology is not None and not topology.geometry_recorded:
        missing = ", ".join(topology.reproducibility()["missing"])
        print(f"  reproducible : NO — {missing} unrecorded in {args.topology}. Results taken now "
              f"cannot be rebuilt off this bench.")
        log.warning("bench geometry unrecorded (%s): results are not reproducible off this bench", missing)
    if jammer_enabled:
        avail = "hackrf_transfer found" if jammer.available() else "hackrf_transfer NOT on PATH"
        print(f"  jammer       : ARMABLE by the operator ({avail}; {jammer_why}) — "
              f"RESEARCH USE ONLY")
    elif args.jammer:
        print(f"  jammer       : REFUSED — {jammer_why}")
    else:
        print(f"  jammer       : disabled (start with --jammer to allow one)")
    # Tier B. Runs after the banner's device lines so the operator can already see WHICH radios
    # are about to transmit, and only on request: identity is free to check, continuity is not.
    if args.verify_path and worker is not None:
        from gr_autopilot.hardware.path_check import verify_path
        cal = (topology.common.get("calibration") or {}) if topology is not None else {}
        # Check on the bench's declared operating channel, not a hardcoded default:
        # nothing has been tuned yet at startup, and 2.4 GHz may be somebody else's.
        path_result = verify_path(service, jammer=jammer,
                                  expected_snr_db=cal.get("expected_snr_db"),
                                  link_freq_hz=(topology.common.get("demo_center_freq_hz")
                                                if topology is not None else None))
        path_check["result"] = path_result
        verdict = "verified by transmitting" if path_result.ok else "** FAILED **"
        print(f"  path         : {verdict}  (link {path_result.link_snr_db} dB"
              + (f", jammer +{path_result.jammer_margin_db} dB median, "
                 f"spread {path_result.jammer_margin_spread_db} dB"
                 if path_result.jammer_margin_db is not None else "") + ")")
        for finding in path_result.findings:
            log.warning("path check: %s", finding)
    elif args.verify_path:
        print("  path         : not checked — no radios attached")

    print(f"  operator     : POST {url}/control  (the hidden channel and the interferer)")
    print(f"                 " + ("same --token as /mcp; loopback is exempt"
                                  if args.token else
                                  "UNGUARDED — no --token set, so anything that reaches this port "
                                  "can drive it"))
    print(f"  log          : {log_path or 'console only'}   (console at {args.log_level}; "
          f"--log-level debug for every request)")
    print(f"\n  claude mcp add --transport http gr-autopilot {url}/mcp\n")
    sys.stdout.flush()
    try:
        # ``snapshot`` (the defaulted path), NOT args.snapshot: the writer above is bound to the
        # defaulted path, and handing the reader the raw flag instead would point the two at
        # different files -- /data would answer "snapshot": null forever while snapshots piled up
        # on disk, which looks exactly like the writer being broken.
        serve(lambda: service.ledger_db_path, lambda: snapshot or service.snapshot_path,
              port=args.port, host=args.host,
              mcp=endpoint, artifacts_dir=lambda: service.artifacts_dir, devices=device_report,
              control=control,
              topology=(lambda: topology.status(topo_findings)) if topology else None,
              experiments=service)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if worker is not None:
            worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
