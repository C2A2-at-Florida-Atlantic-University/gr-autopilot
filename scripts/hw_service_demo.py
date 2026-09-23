#!/usr/bin/env python3
"""Drive the real PlutoSDR link through the AGENT TOOL SURFACE (AutopilotService, spec §8) --
the same methods an LLM agent calls over MCP. Walks discover -> construct -> claim -> execute
-> measure on hardware, and shows the framework never leaks the hidden channel (no SNR in
get_status / get_metrics beyond the framework-measured estimate).

Run:  python scripts/hw_service_demo.py --atten 45
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-uri", default="ip:192.168.2.1")
    ap.add_argument("--rx-uri", default="ip:192.168.3.1")
    ap.add_argument("--atten", type=float, default=45.0, help="hidden TX attenuation (dB)")
    ap.add_argument("--modulation", default="qpsk")
    args = ap.parse_args()

    from gr_autopilot.flowgraph import FlowgraphSpec
    from gr_autopilot.tools.service import AutopilotService
    try:
        from gr_autopilot.link.pluto import PlutoBackend
    except Exception as exc:  # pragma: no cover
        print(f"cannot import PlutoBackend ({exc})", file=sys.stderr)
        return 2

    be = PlutoBackend(args.tx_uri, args.rx_uri, sample_rate=2_084_000.0)
    # Operator wires the service to hardware and sets the HIDDEN physical condition (gains).
    svc = AutopilotService(
        backend=be, device_backend="pluto",
        channel={"tx_atten_db": args.atten, "rx_gain_db": 40.0},
        ledger_path=Path(tempfile.mkdtemp()) / "svc.jsonl", confirm_bits=8000,
    )

    print("== discover ==")
    for d in svc.list_devices():
        print(f"  {d['device_id']}: {d['uri']}  tuning {d['tuning_range_hz'][0]/1e6:.0f}-"
              f"{d['tuning_range_hz'][1]/1e9:.1f}GHz  chip={d['clock'].get('chip_model')}")
    print(f"  framework picked TX={svc.tx_device}  RX={svc.rx_device} (grader reserved on RX)")

    print("== construct (whole-graph-as-JSON) ==")
    spec = FlowgraphSpec.link(args.modulation, sps=8, rolloff=0.35).to_dict()
    print(f"  build_flowgraph -> {svc.build_flowgraph(spec)}")

    print("== claim ==")
    print(f"  {svc.claim_device(svc.tx_device, 'transmitter')}")
    print(f"  {svc.claim_device(svc.rx_device, 'receiver')}")

    print("== execute on hardware ==")
    print(f"  run_flowgraph -> {svc.run_flowgraph()}")

    print("== measure (framework-owned grader) ==")
    m = svc.get_metrics()
    print(f"  BER={m['BER']:.2e}  EVM={m['EVM']:.1f}%  SNR_est={m['SNR']:.1f}dB  n_bits={m['n_bits']}")
    c = svc.capture_constellation(max_points=8)
    print(f"  constellation[{args.modulation}] first pts: " +
          " ".join(f"({p[0]:+.2f},{p[1]:+.2f})" for p in c["points"][:4]))

    print("== integrity: hidden channel must NOT leak ==")
    st = svc.get_status()
    leaked = [k for k in ("es_n0_db", "tx_atten_db", "rx_gain_db", "snr", "SNR") if k in str(st)]
    print(f"  get_status keys: {sorted(st)}")
    print(f"  channel leak in status? {'YES -> BUG' if leaked else 'no'}")

    print("== edit ledger ==")
    print("  " + svc.read_edit_ledger().replace("\n", "\n  "))
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
