"""The request and reply format spoken between the daemon and a radio worker process.

Newline-delimited JavaScript Object Notation over the worker's standard input and output -- the
same shape the tool server already uses, so there is one framing idiom in the project rather than
two. Numeric arrays (payload bits, recovered symbols) are carried as base64-encoded raw buffers
rather than lists of numbers: a trial produces tens of thousands of values, and encoding those as
decimal text would cost more than the measurement itself, while a raw buffer round-trips exactly.

Keeping this in its own module means the parent and the child agree on one definition, and a test
can exercise the encoding without starting a process.
"""
from __future__ import annotations

import base64
import json

import numpy as np

# Requests the daemon may send.
RUN_LINK = "run_link"
SENSE = "sense_spectrum"
SET_CONDITION = "set_condition"
DEVICE_HEALTH = "device_health"
PING = "ping"
CLOSE = "close"


def encode_array(arr) -> dict:
    """A numpy array as a JSON-safe object preserving dtype and shape exactly."""
    a = np.ascontiguousarray(arr)
    return {"__ndarray__": base64.b64encode(a.tobytes()).decode("ascii"),
            "dtype": str(a.dtype), "shape": list(a.shape)}


def decode_array(obj) -> np.ndarray:
    raw = base64.b64decode(obj["__ndarray__"])
    return np.frombuffer(raw, dtype=np.dtype(obj["dtype"])).reshape(obj["shape"])


def _default(o):
    if isinstance(o, np.ndarray):
        return encode_array(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, complex):
        return {"__complex__": [o.real, o.imag]}
    # Anything else is a BUG, and stringifying it hides the bug inside a valid-looking message.
    #
    # This fallback used to be ``str(o)``. A HopPlan set on the worker-backed radios therefore
    # crossed as the text "HopPlan(channels=(...), hop_rate_hz=4.0)", PlutoBackend.set_condition
    # ignored the unrecognised key, and set_hop_plan became a silent no-op: the link never hopped
    # while every tool reported success and the following jammer chased a schedule the link was
    # not flying. A whole Stage-3 measurement can be taken, and believed, against a defence that
    # never ran. Refusing to encode what the other end cannot decode is the only way that failure
    # shows up as an error instead of as a result.
    raise TypeError(
        f"{type(o).__name__} cannot cross the worker protocol; encode it explicitly "
        f"(the protocol carries JSON, numpy arrays/scalars and complex numbers)")


def _object_hook(d):
    if "__ndarray__" in d:
        return decode_array(d)
    if "__complex__" in d:
        return complex(*d["__complex__"])
    return d


def dumps(obj) -> str:
    return json.dumps(obj, default=_default, allow_nan=False)


def loads(text: str):
    return json.loads(text, object_hook=_object_hook)
