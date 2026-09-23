#!/usr/bin/env python3
"""How high does the modulation ladder go on THIS link?

Two steps, both through the running daemon's own surfaces -- the agent's tools over /mcp for
building and measuring, the operator's POST /control (same bearer token) for the link budget:

  1. The error-vector floor: BPSK, N trials per transmit-attenuation step, full-length grades
     only. A floor that keeps falling as attenuation drops is thermal and power buys rungs; one
     that flattens is impairment and no power will reach the high QAMs.
  2. The ladder at the best point: every rung above BPSK, N trials each, full-length grades
     counted separately from short ones, a diagnosis per rung, stop after the first rung whose
     full-length trials all fail the target.

The bench is restored to its starting attenuation afterwards unless --keep is given. Results go
into the selected experiment's directory as JSON and the ledger gets one annotation.

    GRA_TOKEN=<the daemon's --token, only needed off the bench host> \\
    python3 scripts/hw_ladder_ceiling.py --experiment "ladder ceiling 2370" \\
        --attens 38,34,30,26,22,18 --trials 6 --n-bits 60000

This TRANSMITS. Research use only, on an attested contained bench (RESPONSIBLE-USE.md).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import time
import urllib.request

SHORT = {"bpsk": "bpsk", "qpsk": "qpsk", "8psk": "psk8", "16qam": "qam16",
         "32qam": "qam32", "64qam": "qam64", "256qam": "qam256"}
LADDER = ["qpsk", "8psk", "16qam", "32qam", "64qam", "256qam"]


class Daemon:
    def __init__(self, url: str, token: str = ""):
        self.url, self.op = url, token
        self.h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "hw_ladder_ceiling", "version": "1"}})

    def rpc(self, method: str, params: dict) -> dict:
        req = urllib.request.Request(self.url + "/mcp", headers=self.h, data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode())
        r = urllib.request.urlopen(req, timeout=180)
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.h["Mcp-Session-Id"] = sid
        body = r.read().decode()
        for line in body.splitlines():          # tolerate SSE framing
            if line.startswith("data: "):
                body = line[6:]
                break
        return json.loads(body)

    def call(self, tool: str, **args):
        r = self.rpc("tools/call", {"name": tool, "arguments": args})["result"]
        out = json.loads(r["content"][0]["text"])
        if r.get("isError"):
            raise RuntimeError(f"{tool}: {out.get('error')}")
        return out

    def control(self, **cmd) -> dict:
        req = urllib.request.Request(self.url + "/control", method="POST", data=json.dumps(cmd).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.op}"} if self.op else {})})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())


def spec(mod: str) -> dict:
    return {"structure_id": f"{mod}_ceiling", "modulation": mod,
            "tx_chain": [f"{SHORT[mod]}_mod", "rrc_pulse_shape"],
            "rx_chain": ["agc", "rrc_matched_filter", "symbol_sync", "costas_carrier", f"{SHORT[mod]}_demod"],
            "pulse_shape": {"type": "rrc", "sps": 4, "rolloff": 0.35}, "sync": {"timing": "symbol_sync"}}


def trials(d: Daemon, n: int, n_bits: int) -> list[dict]:
    rows = []
    for _ in range(n):
        d.call("run_flowgraph", n_bits=n_bits)
        m = d.call("get_metrics")
        rows.append({"ber": m["BER"], "evm": m["EVM"], "snr": m["SNR"], "n_bits": m["n_bits"],
                     "errors": m["n_errors"], "full": m["n_bits"] >= n_bits - 8})
    return rows


def summarize(rows: list[dict]) -> dict:
    full = [r for r in rows if r["full"]]
    short = [r for r in rows if not r["full"]]
    out = {"n": len(rows), "n_full": len(full),
           "short_errors": sum(r["errors"] for r in short), "short_bits": sum(r["n_bits"] for r in short)}
    if full:
        bits = sum(r["n_bits"] for r in full)
        out.update(full_errors=sum(r["errors"] for r in full), full_bits=bits,
                   full_ber=sum(r["errors"] for r in full) / bits,
                   full_ber_max=max(r["ber"] for r in full),
                   evm_mean=st.mean(r["evm"] for r in full), snr_mean=st.mean(r["snr"] for r in full),
                   snr_sd=(st.stdev(r["snr"] for r in full) if len(full) > 1 else 0.0))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--experiment", required=True, help="experiment to start or switch to")
    ap.add_argument("--attens", default="38,34,30,26,22,18", help="tx_atten_db steps for the floor sweep")
    ap.add_argument("--trials", type=int, default=6)
    ap.add_argument("--n-bits", type=int, default=60_000,
                    help="bits per trial; 60000 is divisible by every bits-per-symbol, so 'full' means full")
    ap.add_argument("--target", type=float, default=1e-2)
    ap.add_argument("--rx-gain", type=float, default=None, help="rx_gain_db to hold during the run")
    ap.add_argument("--restore-atten", type=float, default=38.0)
    ap.add_argument("--keep", action="store_true", help="leave the bench at the best point")
    args = ap.parse_args(argv)
    # Loopback is exempt from the bearer token, so a run on the bench host needs nothing set.
    op = os.environ.get("GRA_TOKEN", "")

    d = Daemon(args.url, op)
    cur = d.call("current_experiment")
    if not (cur.get("selected") and cur.get("slug") == args.experiment.lower().replace(" ", "-")):
        try:
            r = d.call("start_experiment", name=args.experiment,
                       goal="ladder ceiling: EVM floor vs tx attenuation, then every rung at the best point")
        except RuntimeError:
            r = d.call("switch_experiment", name=args.experiment)
        print(f"{r['action']} {r['experiment']['slug']} | reset: {r['reset']}")
    exp_dir = d.call("current_experiment")["dir"]
    d.call("claim_device", device_id="pluto-a", role="transmitter")
    d.call("claim_device", device_id="pluto-b", role="receiver")
    if args.rx_gain is not None:
        d.control(rx_gain_db=float(args.rx_gain))

    attens = [float(a) for a in args.attens.split(",")]
    results = {"attens": attens, "trials": args.trials, "n_bits": args.n_bits, "rx_gain": args.rx_gain,
               "floor": [], "ladder": []}
    print("\n== 1. error-vector floor vs transmit attenuation (BPSK, full-length trials) ==")
    d.call("build_flowgraph", spec=spec("bpsk"))
    for att in attens:
        d.control(tx_atten_db=att)
        time.sleep(1.0)
        rows = trials(d, args.trials, args.n_bits)
        s = summarize(rows)
        results["floor"].append({"tx_atten_db": att, "rows": rows, **s})
        if s["n_full"]:
            print(f"  {att:5.1f} dB: EVM {s['evm_mean']:5.2f}%  SNR {s['snr_mean']:5.2f} dB (sd {s['snr_sd']:.2f})"
                  f"  errors {s['full_errors']}/{s['full_bits']}  full {s['n_full']}/{s['n']}")
        else:
            print(f"  {att:5.1f} dB: no full-length grades in {s['n']} trials")

    with_full = [r for r in results["floor"] if r["n_full"]]
    best = min(with_full, key=lambda r: r["evm_mean"]) if with_full else results["floor"][0]
    print(f"\nbest point: tx_atten {best['tx_atten_db']} dB")
    d.control(tx_atten_db=best["tx_atten_db"])
    time.sleep(1.0)

    print("\n== 2. the ladder at the best point ==")
    top = "bpsk"
    for mod in LADDER:
        d.call("build_flowgraph", spec=spec(mod))
        rows = trials(d, args.trials, args.n_bits)
        s = summarize(rows)
        try:
            diag = d.call("diagnose_signal", render_path=os.path.join(exp_dir, "flowgraphs", f"{mod}_ceiling.png"))
        except RuntimeError as exc:
            diag = {"fault": f"unavailable: {exc}", "features": {}}
        passed = bool(s["n_full"]) and s["full_ber_max"] <= args.target
        results["ladder"].append({"mod": mod, "rows": rows, **s, "pass": passed,
                                  "diagnosis": diag.get("fault"), "features": diag.get("features")})
        if s["n_full"]:
            print(f"  {mod:6s}: BER {s['full_ber']:.2e} (max {s['full_ber_max']:.1e}, {s['full_errors']}/{s['full_bits']})"
                  f"  EVM {s['evm_mean']:5.2f}%  SNR {s['snr_mean']:5.2f} dB (sd {s['snr_sd']:.2f})"
                  f"  full {s['n_full']}/{s['n']}  short errs {s['short_errors']}/{s['short_bits']}"
                  f"  diag {diag.get('fault')}  {'PASS' if passed else 'FAIL'}")
        else:
            print(f"  {mod:6s}: no full-length grades ({s['n']} trials, short errs {s['short_errors']}/{s['short_bits']})  FAIL")
        if passed:
            top = mod
        elif s["n_full"] and all(r["ber"] > args.target for r in rows if r["full"]):
            print(f"  stopping: every full-length {mod} trial fails {args.target:g}")
            break
    results["top_rung"] = top

    out = os.path.join(exp_dir, "ladder_ceiling.json")
    with open(out, "w") as fh:
        json.dump(results, fh, indent=1)
    led = d.call("read_edit_ledger", compact=False)
    note = (f"LADDER CEILING ({args.trials} trials/rung, {args.n_bits} bits, rx_gain {args.rx_gain}). Floor: "
            + ", ".join(f"{r['tx_atten_db']:g}dB->{r.get('evm_mean', float('nan')):.1f}%/{r.get('snr_mean', float('nan')):.1f}dB"
                        for r in results["floor"])
            + f". Best {best['tx_atten_db']:g} dB. Ladder: "
            + ", ".join(f"{r['mod']} {'PASS' if r['pass'] else 'FAIL'}"
                        + (f" (BER {r['full_ber']:.1e}, EVM {r['evm_mean']:.1f}%, SNR sd {r['snr_sd']:.2f})" if r["n_full"] else " (no full grades)")
                        for r in results["ladder"])
            + f". Top rung meeting {args.target:g}: {top}. Data: {out}.")
    d.call("annotate_ledger", iteration=led[-1]["iteration"] if led else 1, note=note)
    if not args.keep:
        d.control(tx_atten_db=args.restore_atten)
        print(f"\nbench restored: tx_atten {args.restore_atten:g} dB")
    print("wrote", out, "| top rung:", top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
