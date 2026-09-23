"""Checking whether a radio is still there, without disturbing what it is doing.

One function, used from two places: inside the worker process, which holds the radios during an
experiment, and from the daemon after a worker has died, when nobody holds them and each device
must be judged on its own.

The method is a single attribute read over a short-lived, separate connection. Two alternatives
look reasonable and are not, both measured on the bench:

* Capturing samples over a second connection SUCCEEDS -- the device server multiplexes it -- while
  silently taking two whole buffers away from the capture already in progress. A health check
  that quietly corrupts the measurement it is meant to be watching is worse than none.
* The project's stand-alone health script WRITES tuning frequency, gain mode, sample rate and
  gain. Run periodically it would overwrite whatever the experiment had just configured, and the
  resulting bad measurement would look like physics rather than like a probe.

An attribute read costs a few milliseconds, changes nothing, and takes no samples.
"""
from __future__ import annotations

import subprocess

#: Seconds to wait for a device to answer. A radio that has gone silent stops answering rather
#: than refusing, so the timeout -- not an error -- is what identifies it.
PROBE_TIMEOUT_S = 4.0


def probe_uri(uri: str, timeout: float = PROBE_TIMEOUT_S) -> dict:
    """Is the radio at ``uri`` answering? Returns ``{alive, ...}`` and never raises."""
    try:
        proc = subprocess.run(
            ["iio_attr", "-u", uri, "-c", "ad9361-phy", "temp0", "input"],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"alive": False, "reason": "no answer within the probe timeout"}
    except OSError as exc:
        return {"alive": False, "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"alive": False, "reason": (proc.stderr or "").strip().splitlines()[-1][:120]
                if (proc.stderr or "").strip() else f"probe exited {proc.returncode}"}
    lines = (proc.stdout or "").strip().splitlines()
    out = {"alive": True}
    try:
        out["temp_c"] = round(int(lines[-1]) / 1000.0, 1)
    except (ValueError, IndexError):
        pass
    return out


def probe_devices(uris: dict[str, str], timeout: float = PROBE_TIMEOUT_S) -> dict:
    """Probe several devices by role, e.g. ``{"tx": "ip:...", "rx": "ip:..."}``."""
    return {role: {"uri": uri, **probe_uri(uri, timeout)} for role, uri in uris.items() if uri}
