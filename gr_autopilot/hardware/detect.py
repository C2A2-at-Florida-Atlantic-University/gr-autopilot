"""Device detection (spec §5.3).

``detect_devices`` returns capability manifests. The default MOCK backend (two Plutos) keeps
the tool surface and claim/loop logic runnable with no radios. The ``"pluto"`` backend scans
attached devices with libiio and reads each device's *live* attributes — critical because the
bench is asymmetric: one unit is hacked to AD9364 (70 MHz-6 GHz), the other is a vanilla
AD9363A (325 MHz-3.8 GHz). The tuning range, chip model, serial, firmware and clock trim are
read from the hardware, never assumed.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import replace

from gr_autopilot.hardware.manifest import (
    HACKED_PLUTO_QUIRKS,
    VANILLA_PLUTO_QUIRKS,
    DeviceManifest,
    pluto_manifest,
)

log = logging.getLogger(__name__)


class DeviceProbeError(RuntimeError):
    """An libiio CLI call for one device failed (non-zero exit or timeout). Recoverable: the scan
    skips the offending unit rather than aborting or emitting a defaulted (wrong-chip) manifest."""


def _mock_plutos() -> list[DeviceManifest]:
    """Two vanilla Plutos on USB, matching the Stage-1 bench (spec §5.3 / §7)."""
    return [
        pluto_manifest("pluto-a", uri="usb:1.4.5", serial="mock-A-0001", simulated=True),
        pluto_manifest("pluto-b", uri="usb:1.5.5", serial="mock-B-0002", simulated=True),
    ]


def _run(cmd: list[str], timeout: float = 15.0, check: bool = False) -> str:
    """Run an libiio CLI and return stdout. Raises DeviceProbeError on TIMEOUT always, and on a
    non-zero exit when ``check`` (so a hung/erroring device is skipped, never silently defaulted).
    FileNotFoundError (binary absent) propagates for the caller to translate."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DeviceProbeError(f"{cmd[0]} timed out after {timeout:g}s") from exc
    if check and p.returncode != 0:
        raise DeviceProbeError(f"{cmd[0]} exited {p.returncode}: {p.stderr.strip()[:200]}")
    return p.stdout


def _parse_scan_uris(text: str) -> list[str]:
    """Pure parse of ``iio_info -s`` output: one URI per physical device, deduped by serial,
    an ``ip:`` URI preferred over ``usb:`` when both are present for the same unit."""
    best: dict[str, str] = {}
    for line in text.splitlines():
        m = re.search(r"serial=(\S+)\s+\[([^\]]+)\]", line)
        if not m:
            continue
        serial, uri = m.group(1), m.group(2)
        if serial not in best or (uri.startswith("ip:") and not best[serial].startswith("ip:")):
            best[serial] = uri
    return list(best.values())


def _parse_context_attrs(text: str) -> dict[str, str]:
    """Pure parse of ``iio_attr -u <uri> -C`` output into a ``key -> value`` dict."""
    attrs: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("IIO context") or ": " not in line:
            continue
        key, _, val = line.partition(": ")
        attrs[key.strip()] = val.strip()
    return attrs


def _scan_pluto_uris() -> list[str]:
    try:
        out = _run(["iio_info", "-s"])
    except FileNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("libiio CLI (iio_info) not found on PATH") from exc
    return _parse_scan_uris(out)


def _context_attrs(uri: str) -> dict[str, str]:
    # check=True: a device whose attribute read errors must be skipped, not turned into a manifest
    # that (with empty attrs) would default to model='' -> hacked=False -> the wrong 325 MHz floor.
    return _parse_context_attrs(_run(["iio_attr", "-u", uri, "-C"], check=True))


def _as_int(s: str | None) -> int | None:
    try:
        return int(s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _manifest_from_uri(uri: str) -> DeviceManifest:
    a = _context_attrs(uri)
    model = a.get("ad9361-phy,model", "").lower()          # ad9364 | ad9363a | ad9363
    hacked = "9364" in model                                # AD9364 == extended tuning
    ip = a.get("ip,ip-addr")
    canon_uri = f"ip:{ip}" if ip else a.get("uri", uri)
    host = re.match(r"ip:([A-Za-z][\w-]*)\.local", uri)     # mDNS name -> device id
    serial = a.get("hw_serial", "unknown")
    device_id = host.group(1) if host else f"pluto-{serial[:6]}"
    return DeviceManifest(
        device_id=device_id,
        driver="iio",
        uri=canon_uri,
        serial=serial,
        tuning_range_hz=[70e6, 6e9] if hacked else [325e6, 3.8e9],
        sample_rate_range=[521e3, 61.44e6],
        usable_bandwidth_hz=None,
        tx={"channels": 1, "gain_range_db": [-89.0, 0.0]},
        rx={"channels": 1, "gain_modes": ["manual", "slow_attack", "fast_attack"],
            "gain_range_db": [-3.0, 71.0]},
        clock={"internal_ppm": 25, "ext_ref": False,
               "chip_model": model or None,
               "fw_version": a.get("fw_version"),
               "xo_correction": _as_int(a.get("ad9361-phy,xo_correction"))},
        duplex="full",
        quirks=list(HACKED_PLUTO_QUIRKS if hacked else VANILLA_PLUTO_QUIRKS),
    )


def _real_plutos(uris: list[str] | None = None) -> list[DeviceManifest]:
    """Scan attached Plutos and build manifests from their live attributes."""
    if uris is None:
        uris = _scan_pluto_uris()
    if not uris:
        raise RuntimeError("no IIO devices found (iio_info -s returned none); "
                           "pass explicit uris=[...] if the radios use a non-discoverable URI")
    manifests = []
    for u in uris:
        try:
            manifests.append(_manifest_from_uri(u))
        except DeviceProbeError as exc:          # skip one flaky/hung unit, don't abort the whole scan
            log.warning("skipping device %s: %s", u, exc)
    if not manifests:
        raise RuntimeError(f"all {len(uris)} discovered device(s) failed to probe; see warnings")
    return manifests


def detect_bench(tx_uri: str, rx_uri: str,
                 ids: tuple[str, str] = ("pluto-a", "pluto-b")) -> list[DeviceManifest]:
    """Manifests for the two radios at ``tx_uri`` and ``rx_uri``, bound to the TX and RX roles.

    Used when the operator has named the radios on the command line, which is the only moment the
    role of each unit is actually known. Three failure modes it exists to rule out, all of them
    silent under the obvious alternatives:

    * **Role inversion.** A bare scan returns units in discovery order, which is arbitrary --
      on this bench it lists the RX first -- so binding TX to ``devices[0]`` labels the pair
      backwards while the worker transmits on the other one. The URIs the worker was given are
      the only authority on which radio is which, so they are what is used here.
    * **Identity collapse.** :func:`_manifest_from_uri` derives a device id from an
      ``ip:<name>.local`` URI and otherwise falls back to a serial prefix. Two Plutos from one
      production batch share that prefix (both units here begin ``104473``), so a numeric-URI
      scan yields two manifests with one id, and any dict keyed by device id silently keeps one
      radio. Role ids are assigned here instead of derived, so the collision cannot arise.
    * **Both roles on one radio.** A repeated or aliased URI is caught by serial, not by string
      equality, so ``ip:pluto2.local`` and ``ip:192.168.2.1`` are recognised as one unit.

    The ids are LOGICAL role labels; every fact of identity (serial, chip model, firmware, clock
    trim, tuning range, quirks) is read from the hardware and kept.
    """
    tx, rx = _manifest_from_uri(tx_uri), _manifest_from_uri(rx_uri)
    if tx.serial == rx.serial:
        raise RuntimeError(
            f"tx-uri {tx_uri!r} and rx-uri {rx_uri!r} are the same radio (serial {tx.serial}); "
            "the link needs two distinct units")
    return [replace(tx, device_id=ids[0]), replace(rx, device_id=ids[1])]


def detect_devices(backend: str = "mock", uris: list[str] | None = None) -> list[DeviceManifest]:
    """Enumerate attached SDRs into capability manifests.

    Args:
        backend: "mock" (default, no hardware) or "pluto" (live libiio scan).
        uris: optional explicit IIO URIs for the "pluto" backend (skips the scan).
    """
    if backend == "mock":
        return _mock_plutos()
    if backend in ("pluto", "iio", "real"):
        return _real_plutos(uris)
    raise ValueError(f"unknown detection backend {backend!r}")
