"""LinkBackend interface + parameter/result types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np


@dataclass
class LinkParams:
    """One link configuration.

    Fields are a superset across backends; each backend uses the ones it understands and
    ignores the rest (documented per field). Continuous knobs here are the ones the BO
    inner loop will eventually tune; ``modulation`` is a structural choice for the LLM.
    """

    modulation: str = "qpsk"            # bpsk | qpsk | 16qam (structural)
    n_payload_bits: int = 4096          # length of the known payload
    sps: int = 4                        # samples per symbol (pulse-shaped backends)
    rolloff: float = 0.35               # RRC excess bandwidth (pulse-shaped backends)

    # The signal-processing chains the agent authored, in order. Backends with a fixed internal
    # pipeline (NumpySimBackend, PlutoBackend) ignore these, exactly as they ignore any other
    # field they do not understand. GRSpecBackend EXECUTES them: each name becomes a real in-tree
    # GNU Radio block, so the agent's structural choices change the measurement. Kept as tuples
    # because LinkParams is hashed/compared in places and a list would not be hashable.
    tx_chain: tuple[str, ...] = ()
    rx_chain: tuple[str, ...] = ()

    # Normalized loop bandwidth for symbol timing recovery. None leaves the backend's own
    # default. Present here so that editing it in an exported GNU Radio Companion file and
    # handing the file back actually changes the measurement, rather than the value being fixed
    # when the backend was constructed.
    timing_loop_bw: float | None = None

    # Channel condition.
    es_n0_db: float | None = None       # NumpySimBackend: exact Es/N0 in dB
    noise_voltage: float | None = None  # GrSimBackend: channel_model noise std
    freq_offset_hz: float = 0.0         # carrier frequency offset (Hz)
    sample_rate: float = 1_000_000.0    # sample rate (Hz), for offset<->cycles/sample
    phase_offset_rad: float = 0.0       # residual constant carrier phase (channel, NumpySim)

    # Continuous knobs the BO inner loop tunes (spec §4.2).
    phase_correction_rad: float = 0.0   # de-rotation applied at the receiver

    # Forward error correction (the "C" in AMC). None = uncoded; "conv_k3_r12" = the framework
    # K=3 rate-1/2 convolutional code. Backends that understand it encode the info payload before
    # modulation and Viterbi-decode after; BER is then the post-decode info BER.
    coding: str | None = None
    soft_decision: bool = False         # decode coded links from demodulator LLRs (~2 dB gain) vs hard bits

    # Reproducibility.
    prbs_order: int = 15
    seed: int = 1


@dataclass
class LinkResult:
    """Aligned outcome of one trial, ready for the scoring layer.

    ``rx_syms`` and ``ref_syms`` are aligned one-to-one with ``tx_syms``; ``rx_bits`` is
    aligned to ``tx_bits``. ``ref_syms`` is the DATA-AIDED reference for EVM/SNR — the KNOWN
    transmitted symbols (== ``tx_syms``), not the receiver's decisions. Referencing the decisions
    would make EVM/SNR a decision-directed MER, optimistically biased at low SNR; the framework owns
    the payload, so it grades against the truth. (rx_syms is gain/phase-normalized to that scale.)
    """

    modulation: str
    tx_bits: np.ndarray
    rx_bits: np.ndarray
    tx_syms: np.ndarray
    rx_syms: np.ndarray
    ref_syms: np.ndarray
    meta: dict = field(default_factory=dict)


class LinkBackend(ABC):
    """Runs one TX->channel->RX trial for a given LinkParams."""

    #: Human-readable backend name.
    name: str = "link-backend"

    #: True if the backend OWNS the channel condition as device/model state (set via
    #: ``set_condition``) rather than reading it from each ``LinkParams``. Hardware backends
    #: set gains on the radio; sim backends take the condition (es_n0_db, ...) in the params.
    owns_channel: bool = False

    #: Condition keys this backend actually APPLIES to the link it runs.
    #:
    #: ``set_condition`` stays deliberately tolerant — a sim-style channel dict must not crash a
    #: radio — but tolerance is only safe when something else can tell a key that was ignored
    #: because it was irrelevant from a key that was ignored because the feature does not exist
    #: here. Nothing could, and ``set_hop_plan`` spent a whole experiment reporting success on a
    #: backend that cannot hop. The agent-facing actions in ``AutopilotService`` check their knob
    #: against this set and refuse rather than pretend. Empty means "unknown/declares nothing",
    #: which is not checked — declare it on any backend an agent can drive.
    applies_condition: frozenset = frozenset()

    @abstractmethod
    def run_link(self, params: LinkParams) -> LinkResult:
        """Execute one trial and return an aligned LinkResult."""
        raise NotImplementedError

    def set_condition(self, **cond) -> None:
        """Apply the operator/framework-set (hidden) channel condition. No-op for backends
        that read the condition from ``LinkParams`` (``owns_channel=False``)."""
        return None

    def sense_spectrum(self, freqs_hz, bw_hz=None):
        """Monitor role (spec §6): framework-owned energy detection. With the link silent,
        measure received power at each candidate frequency. Returns a list of
        ``{center_freq_hz, power_db, occupied}`` (power over the noise floor) or ``None`` if the
        backend has no monitor. Lets the agent *sense* where an interferer is rather than blindly
        probe every channel with a full link trial."""
        return None
