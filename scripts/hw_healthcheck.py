#!/usr/bin/env python3
"""RX-only health check for the bench Plutos — NO transmit.

Configures each radio's receiver (tune / rate / manual gain), captures real IQ via
`iio_readdev`, and reports noise floor + liveness + temperature, so we can tell "healthy
radio" from "fell off the USB bus" before trusting any link measurement (spec §5.3 probe).

Receive only — this never keys a transmitter, so it is safe with antennas attached.
Uses the libiio CLI (iio_attr / iio_readdev); needs no pyadi-iio. Run: python3 scripts/hw_healthcheck.py
"""
from __future__ import annotations

import subprocess
import sys

import numpy as np

DEVICES = [("pluto2", "ip:192.168.2.1"), ("pluto3", "ip:192.168.3.1")]
FREQ_HZ = 2_400_000_000
RATE_HZ = 2_084_000
GAIN_DB = 40
BW_HZ = 2_000_000
N_SAMPLES = 65536
ADC_FULL_SCALE = 2048.0  # 12-bit signed


def _attr(uri, *args):
    subprocess.run(["iio_attr", "-u", uri, *args], capture_output=True, timeout=10)


def _read_attr(uri, *args):
    r = subprocess.run(["iio_attr", "-u", uri, *args], capture_output=True, timeout=10, text=True)
    lines = r.stdout.strip().splitlines()
    return lines[-1] if lines else "?"


def configure_rx(uri):
    _attr(uri, "-o", "-c", "ad9361-phy", "altvoltage0", "frequency", str(FREQ_HZ))
    _attr(uri, "-i", "-c", "ad9361-phy", "voltage0", "gain_control_mode", "manual")
    _attr(uri, "-i", "-c", "ad9361-phy", "voltage0", "sampling_frequency", str(RATE_HZ))
    _attr(uri, "-i", "-c", "ad9361-phy", "voltage0", "rf_bandwidth", str(BW_HZ))
    _attr(uri, "-i", "-c", "ad9361-phy", "voltage0", "hardwaregain", str(GAIN_DB))


def capture_iq(uri, n):
    r = subprocess.run(["iio_readdev", "-u", uri, "-s", str(n), "cf-ad9361-lpc"],
                       capture_output=True, timeout=25)
    raw = np.frombuffer(r.stdout, dtype=np.int16)
    return raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)


def temperature_c(uri):
    v = _read_attr(uri, "-c", "-i", "ad9361-phy", "temp0", "input")
    try:
        return f"{int(v) / 1000:.1f}C"
    except ValueError:
        return "?"


def main():
    print(f"RX-only health check @ {FREQ_HZ/1e9:.2f} GHz, {RATE_HZ/1e6:.3f} Msps, "
          f"gain {GAIN_DB} dB  (NO transmit)\n")
    print(f"{'device':7} {'uri':18} {'temp':6} {'noise dBFS':>11} {'std':>6}  liveness")
    print("-" * 62)
    ok_all = True
    for name, uri in DEVICES:
        try:
            configure_rx(uri)
            iq = capture_iq(uri, N_SAMPLES)
            if iq.size < 2:
                print(f"{name:7} {uri:18} {'-':6} {'-':>11} {'-':>6}  NO DATA")
                ok_all = False
                continue
            nf = 10 * np.log10(np.mean(np.abs(iq) ** 2) / ADC_FULL_SCALE**2 + 1e-20)
            std = float(iq.real.std())
            live = "OK" if std > 1 else "DEAD (flat)"
            ok_all = ok_all and std > 1
            print(f"{name:7} {uri:18} {temperature_c(uri):6} {nf:>11.1f} {std:>6.1f}  "
                  f"{live} ({iq.size} samp)")
        except Exception as e:  # noqa: BLE001
            print(f"{name:7} {uri:18} ERROR: {e}")
            ok_all = False
    print("\nRESULT:", "all radios healthy (RX)" if ok_all else "PROBLEM — see above")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
