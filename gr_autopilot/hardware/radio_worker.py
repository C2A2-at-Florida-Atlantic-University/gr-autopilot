"""The radio worker: a child process that owns the radios, and is allowed to die.

Run as::

    python3 -m gr_autopilot.hardware.radio_worker --backend pluto --tx-uri ip:... --rx-uri ip:...

It reads one request per line on standard input and writes one reply per line on standard output.
Nothing else talks to it; it has no network port and no knowledge of the dashboard or of any
model driving the experiment.

Why this exists as a separate process rather than a thread or an object.

Losing a radio does not fail gracefully. Measured on the bench, a Pluto whose connection goes
SILENT -- which is what pulling the cable looks like, since the network interface disappears and
packets simply stop rather than the connection being closed -- makes the GNU Radio input/output
library throw from one of its own worker threads. Nothing catches it, the C++ runtime calls
terminate, and the whole process is killed with SIGABRT. Python never gets the chance to handle
anything: no exception, no cleanup, no error message. (A cleanly CLOSED connection is survivable;
a silent one is not, and the silent case is the one an unplugged cable produces.)

A related nuisance: the same library's device contexts hang on destruction, which is why the
project's hardware scripts end in ``os._exit``.

So the radio cannot live in the process that must stay up. Isolating it here turns both problems
into ordinary events: this process dying IS the signal that the radio is gone, and its abrupt
exit is normal rather than something to be prevented. The daemon notices, reports the device as
unavailable, and refuses to record a measurement that never happened.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback

from gr_autopilot.hardware.protocol import (
    CLOSE, DEVICE_HEALTH, PING, RUN_LINK, SENSE, SET_CONDITION, dumps, loads)


def _build_backend(args):
    """Construct the backend this worker owns. Imported here, not at module scope, so that
    starting a worker for the simulated backend needs no GNU Radio installed."""
    if args.backend == "pluto":
        from gr_autopilot.hardware.health import probe_devices
        from gr_autopilot.link.pluto import PlutoBackend

        # Confirm the DSP toolkit is importable BEFORE reporting ready. The backend builds its
        # flowgraph lazily, so a worker started under an interpreter without GNU Radio comes up,
        # answers health checks and reports "ready" -- then fails on the first measurement with
        # ModuleNotFoundError. That happened: the worker inherits the daemon's ``sys.executable``,
        # and a daemon launched as bare ``python3`` can resolve to an interpreter that has the
        # radios' libraries but not GNU Radio. Readiness must mean "can measure", not "started".
        try:
            import gnuradio  # noqa: F401
            from gnuradio import blocks, digital, filter  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"this worker cannot measure: {exc}. It is running under {sys.executable!r}, "
                f"which has no GNU Radio. The worker inherits the daemon's interpreter — start "
                f"the daemon with one that can import gnuradio.") from exc

        # Confirm both radios answer too. Constructing the backend against an absent radio
        # succeeds, and the failure would not surface until the first measurement -- by which
        # point an experiment has started and a controller is waiting on a result never coming.
        health = probe_devices({"tx": args.tx_uri, "rx": args.rx_uri})
        missing = {role: d for role, d in health.items() if not d.get("alive")}
        if missing:
            detail = ", ".join(f"{r} at {d['uri']} ({d.get('reason', 'no answer')})"
                               for r, d in missing.items())
            raise RuntimeError(f"radio not reachable: {detail}")
        return PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=args.sample_rate,
                            rx_gain_db=args.rx_gain, tx_atten_db=args.tx_atten,
                            sync_mode=args.sync_mode,
                            pilot_spacing=args.pilot_spacing)
    if args.backend == "sim":
        # Not useful in production -- the simulated backend has no reason to be in a subprocess --
        # but it lets the whole supervisor path be tested end to end without radios.
        from gr_autopilot.link.numpy_sim import NumpySimBackend
        return NumpySimBackend()
    raise ValueError(f"unknown backend {args.backend!r}")


def _result_to_wire(result) -> dict:
    return {
        "modulation": result.modulation,
        "tx_bits": result.tx_bits, "rx_bits": result.rx_bits,
        "tx_syms": result.tx_syms, "rx_syms": result.rx_syms, "ref_syms": result.ref_syms,
        "meta": result.meta,
    }


def handle(backend, request: dict) -> dict:
    """Handle one request. Returns the reply body; raising is caught by the caller."""
    from gr_autopilot.link.backend import LinkParams

    op = request.get("op")
    if op == PING:
        return {"ok": True, "pong": True}
    if op == RUN_LINK:
        params = LinkParams(**(request.get("params") or {}))
        return {"ok": True, "result": _result_to_wire(backend.run_link(params))}
    if op == SENSE:
        rows = backend.sense_spectrum(request.get("freqs_hz") or [], request.get("bw_hz"))
        return {"ok": True, "rows": rows}
    if op == SET_CONDITION:
        backend.set_condition(**(request.get("condition") or {}))
        return {"ok": True}
    if op == DEVICE_HEALTH:
        # Asked of the worker rather than probed from outside, because the worker is the only
        # process holding a context to these radios.
        return {"ok": True, "health": _health(backend)}
    return {"ok": False, "error": {"code": "unknown_op", "message": f"unknown op {op!r}"}}


def _health(backend) -> dict:
    """Per-device liveness, using the side-effect-free probe in
    :mod:`gr_autopilot.hardware.health` (see there for why the obvious alternatives are unsafe)."""
    from gr_autopilot.hardware.health import probe_devices
    return probe_devices({"tx": getattr(backend, "tx_uri", None),
                          "rx": getattr(backend, "rx_uri", None)})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="gr-autopilot radio worker (one process, one radio pair)")
    ap.add_argument("--backend", default="pluto", choices=["pluto", "sim"])
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--sample-rate", type=float, default=2_084_000.0)
    ap.add_argument("--rx-gain", type=float, default=40.0)
    ap.add_argument("--tx-atten", type=float, default=10.0)
    ap.add_argument("--sync-mode", default="zc")
    ap.add_argument("--pilot-spacing", type=int, default=0,
                    help="PSAM: insert a known pilot every N payload symbols and use "
                         "them to resolve the 90-degree carrier ambiguity. 0 = off "
                         "(blind decision-directed loop). Required for coded 16-QAM")
    args = ap.parse_args(argv)

    try:
        backend = _build_backend(args)
    except Exception as exc:  # noqa: BLE001 - report and exit; the parent treats this as unavailable
        sys.stdout.write(dumps({"ok": False, "error": {
            "code": "backend_unavailable", "message": f"{type(exc).__name__}: {exc}"}}) + "\n")
        sys.stdout.flush()
        return 2

    sys.stdout.write(dumps({"ok": True, "ready": True, "backend": getattr(backend, "name", "?")}) + "\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = loads(line)
        except ValueError:
            reply = {"ok": False, "error": {"code": "parse_error", "message": "bad request"}}
        else:
            if request.get("op") == CLOSE:
                break
            try:
                reply = handle(backend, request)
            except Exception as exc:  # noqa: BLE001 - a bad request must not end the session
                reply = {"ok": False, "error": {
                    "code": "worker_error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-800:]}}
        sys.stdout.write(dumps(reply) + "\n")
        sys.stdout.flush()

    # The library's device contexts hang on destruction, so a graceful interpreter shutdown can
    # block for ever. Exiting immediately is the documented way out of that.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
