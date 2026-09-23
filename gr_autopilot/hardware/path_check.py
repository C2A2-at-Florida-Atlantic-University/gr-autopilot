"""Tier-B path verification: is the bench actually wired the way it is declared?

Device *identity* is checkable without transmitting -- serial, chip, firmware, tuning range all
come off the hardware over libiio. Whether the transmitter's energy actually reaches the receiver
does not: it needs a transmission, and the answer is a measurement rather than an assertion.

That gap is not academic. A declaration can name the right radios while the transmitter points at
a wall, an attenuator sits in the wrong arm, or the jammer's antenna is unscrewed -- and every one
of those produces plausible-looking numbers. A silently disconnected jammer is the worst of them,
because a clean band reads exactly like an agent that avoided interference successfully.

Two things are established here, both by transmitting a known signal and measuring what arrives:

* **Link continuity** -- the transmitter reaches the receiver, and by how much margin.
* **Interferer continuity** -- the jammer reaches the receiver, when one is declared.

And one thing is deliberately NOT established: absolute path loss in dB. Neither radio is power
calibrated, so an absolute figure would be invented. What is reported instead is the *received
margin* against the bench's own recorded calibration, which is what actually detects a change:
a bench that measured 19 dB last week and 6 dB today has moved, whatever the true loss is.

Everything here runs through the radio worker, never a side connection: capturing over a second
connection succeeds while silently stealing buffers from the capture in progress (see
:mod:`gr_autopilot.hardware.health`), so a path check that ran alongside an experiment would
corrupt the measurement it was meant to reassure you about. Run it while idle.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

#: A link that cannot reach this many dB of estimated SNR is treated as broken rather than weak.
#: Below it, no modulation in the ladder closes, so the distinction stops mattering.
MIN_LINK_SNR_DB = 3.0

#: How far the measured SNR may fall below the bench's recorded calibration before the path is
#: called changed. Generous, because it must not fire on ordinary drift: measured scatter on the
#: chamber bench is 0.14-2 dB depending on operating point.
DRIFT_TOLERANCE_DB = 6.0

#: A jammer that raises its channel by less than this is not reaching the receiver usefully.
#: The energy detector's own occupancy margin is 6 dB; this sits above it so a pass here means
#: the jammer is not merely detectable but dominant.
MIN_JAMMER_MARGIN_DB = 10.0

#: How many times the keyed interferer is sensed. One sense is one sample of a quantity that is
#: allowed to scatter, and the median of several is what gets judged; the spread is reported.
JAMMER_SENSES = 3

#: A keyed interferer whose senses span more than this is not a usable instrument: a jammer that
#: is only SOMETIMES at the receiver turns 'the agent avoided it' into a coin toss. The 16 %-duty
#: tone bug of 2026-09-08 read +21 dB on one sense and +3 dB on the next; a single sense passed or
#: failed the bench at random, and a spread check would have named it on the first start.
MAX_JAMMER_SPREAD_DB = 6.0


@dataclass
class PathCheck:
    """What one verification pass established, and what it could not."""

    link_ok: bool = False
    link_snr_db: float | None = None
    link_ber: float | None = None
    expected_snr_db: float | None = None
    jammer_ok: bool | None = None            # None when no jammer is declared
    jammer_margin_db: float | None = None            # median over JAMMER_SENSES senses
    jammer_margin_spread_db: float | None = None     # max - min over those senses
    findings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.link_ok and self.jammer_ok is not False

    def to_dict(self) -> dict:
        return {"ok": self.ok, "link_ok": self.link_ok, "link_snr_db": self.link_snr_db,
                "link_ber": self.link_ber, "expected_snr_db": self.expected_snr_db,
                "jammer_ok": self.jammer_ok, "jammer_margin_db": self.jammer_margin_db,
                "jammer_margin_spread_db": self.jammer_margin_spread_db,
                "findings": list(self.findings)}


def verify_path(service, jammer=None, expected_snr_db: float | None = None,
                n_bits: int = 20_000, jammer_if_gain: int = 20,
                link_freq_hz: float | None = None) -> PathCheck:
    """Transmit and measure. ``service`` is the AutopilotService; ``jammer`` the operator's
    instrument, or None to skip the interferer arm.

    Returns a :class:`PathCheck` rather than raising: a failed path check is a finding about the
    bench, and the caller decides whether that stops the session.
    """
    from gr_autopilot.flowgraph import build_and_run
    from gr_autopilot.flowgraph.spec import FlowgraphSpec
    from gr_autopilot.scoring.metrics import compute_metrics

    out = PathCheck(expected_snr_db=expected_snr_db)

    # --- link arm: the most robust rung there is, so a failure means the PATH, not the choice ---
    spec = FlowgraphSpec.link("bpsk", sps=4, rolloff=0.35)
    try:
        r = build_and_run(spec, service.backend, n_payload_bits=n_bits,
                          **service._channel_kwargs())
        m = compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms)
        out.link_snr_db = round(float(m.snr_db), 2)
        out.link_ber = float(m.ber)
        out.link_ok = float(m.snr_db) >= MIN_LINK_SNR_DB
        if not out.link_ok:
            out.findings.append(
                f"transmitter does not reach the receiver: BPSK measured {m.snr_db:.1f} dB "
                f"(floor {MIN_LINK_SNR_DB:g} dB). Check the antennas/cabling and that the two "
                f"radios are the ones the topology names.")
        elif expected_snr_db is not None and float(m.snr_db) < expected_snr_db - DRIFT_TOLERANCE_DB:
            out.findings.append(
                f"the path has changed: {m.snr_db:.1f} dB measured against {expected_snr_db:.1f} dB "
                f"recorded for this bench, a {expected_snr_db - float(m.snr_db):.1f} dB shortfall. "
                f"Geometry, antennas or attenuation is not what the calibration was taken with.")
    except Exception as exc:  # noqa: BLE001 - a failed check is a finding, not a crash
        out.findings.append(f"link check could not run: {exc}")
        return out

    # --- interferer arm: does the declared jammer actually reach the receiver? ------------------
    if jammer is None or not getattr(jammer, "enabled", False):
        return out

    freq = float(link_freq_hz or service.current_link_freq() or 2.4e9)
    # Sense the jammer's channel ALONGSIDE references, never alone. Occupancy is reported relative
    # to the median candidate, so a single-frequency scan is degenerate by construction: the one
    # channel is its own median and always reads 0.00 dB, however loud it actually is. Asking for
    # one frequency is the natural thing to do here and silently measures nothing.
    refs = [freq - 6e6, freq, freq + 6e6, freq + 12e6]
    margins: list[float] = []
    try:
        jammer.apply({"jammer": True, "jammer_freq_hz": freq + 100e3,
                      "jammer_if_gain": int(jammer_if_gain)})
        for _ in range(JAMMER_SENSES):
            rows = service.backend.sense_spectrum(refs, None)
            hit = next((r for r in (rows or [])
                        if abs(float(r["center_freq_hz"]) - freq) < 1e3), None)
            if hit is None:
                out.jammer_ok = False
                out.findings.append(
                    "interferer check: the sensed rows did not include the link channel")
                return out
            margins.append(max(float(hit.get("power_db") or 0.0),
                               float(hit.get("peak_db") or 0.0)))
    except Exception as exc:  # noqa: BLE001
        out.jammer_ok = False
        out.findings.append(f"interferer check could not run: {exc}")
        return out
    finally:
        try:
            jammer.apply({"jammer": False})
        except Exception:  # noqa: BLE001 - never leave a transmitter keyed by a check
            jammer.shutdown()

    out.jammer_margin_db = round(float(statistics.median(margins)), 2)
    out.jammer_margin_spread_db = round(max(margins) - min(margins), 2)
    steady = out.jammer_margin_spread_db <= MAX_JAMMER_SPREAD_DB
    out.jammer_ok = out.jammer_margin_db >= MIN_JAMMER_MARGIN_DB and steady
    if out.jammer_margin_db < MIN_JAMMER_MARGIN_DB:
        out.findings.append(
            f"the jammer does not reach the receiver: keying it moved the channel by only "
            f"{out.jammer_margin_db:.1f} dB, median of {len(margins)} senses (need "
            f"{MIN_JAMMER_MARGIN_DB:g}). An interferer that is not arriving makes a jammed band "
            f"look clean, and 'the agent avoided it' becomes a result about nothing.")
    elif not steady:
        out.findings.append(
            f"the jammer is unstable at the receiver: {len(margins)} senses spanned "
            f"{out.jammer_margin_spread_db:.1f} dB ({min(margins):+.1f} to {max(margins):+.1f}; "
            f"tolerance {MAX_JAMMER_SPREAD_DB:g}). A jammer that comes and goes turns avoidance "
            f"results into coin tosses: check the HackRF's USB link and that its wave file "
            f"fills whole transfers (interferer.loop_length).")
    return out
