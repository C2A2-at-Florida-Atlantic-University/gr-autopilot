"""Framework-owned scoring path.

This package computes ground-truth link quality (BER / EVM / SNR) against a KNOWN
payload. Per the integrity boundary (spec §2, §7): the agent may build and modify any
flowgraph, but must never be able to modify this code or fabricate the payload it grades
against. Keep this package free of agent-controllable inputs.
"""
from gr_autopilot.scoring.metrics import (
    BerResult,
    Metrics,
    ber,
    bit_errors,
    compute_metrics,
    data_aided_snr_db,
    evm,
    snr_from_evm_db,
)
from gr_autopilot.scoring.payload import known_payload, prbs

__all__ = [
    "BerResult",
    "Metrics",
    "ber",
    "bit_errors",
    "compute_metrics",
    "data_aided_snr_db",
    "evm",
    "snr_from_evm_db",
    "known_payload",
    "prbs",
]
