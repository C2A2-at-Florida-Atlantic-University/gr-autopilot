"""Offline AMC ground truth (spec §10.1): the optimal modcod at each SNR.

A one-time exhaustive sweep establishing, per SNR, the highest-spectral-efficiency
modulation whose BER meets the target — the staircase the agent must rediscover without
being told the SNR, and the reference the headline figure is drawn against.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from gr_autopilot.control.objective import spectral_efficiency
from gr_autopilot.flowgraph import FlowgraphSpec, build_and_run
from gr_autopilot.link.backend import LinkBackend
from gr_autopilot.scoring.metrics import compute_metrics

# The ladder the controllers climb by default: the three rungs measured end to end on the
# radios. FULL_LADDER adds every constellation the framework can simulate; pass it explicitly
# when the question is "how high can this link go", and expect the climb to stop where the
# error-vector floor says.
DEFAULT_LADDER = ("bpsk", "qpsk", "16qam")
FULL_LADDER = ("bpsk", "qpsk", "8psk", "16qam", "32qam", "64qam", "256qam")


@dataclass
class AmcCell:
    snr_db: float
    ber: dict = field(default_factory=dict)      # modulation -> BER
    optimal: str = "none"                        # optimal modcod, or "none" (outage)


def amc_sweep(backend: LinkBackend, snr_db_list, ladder=DEFAULT_LADDER,
              target_ber: float = 1e-3, n_bits: int = 200_000, seed: int = 1) -> list[AmcCell]:
    """Exhaustive modcod x SNR sweep -> optimal modcod per SNR."""
    cells = []
    for snr in snr_db_list:
        ber = {}
        for mod in ladder:
            spec = FlowgraphSpec.link(mod)
            r = build_and_run(spec, backend, n_payload_bits=n_bits, es_n0_db=snr, seed=seed)
            ber[mod] = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms).ber
        meeting = [m for m in ladder if ber[m] <= target_ber]
        optimal = max(meeting, key=spectral_efficiency) if meeting else "none"
        cells.append(AmcCell(snr_db=snr, ber=ber, optimal=optimal))
    return cells
