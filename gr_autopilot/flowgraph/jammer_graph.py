"""The interferer as a GNU Radio flowgraph, and the first hardware experiment built this way.

Until now the jammer was the one component that was not GNU Radio. It drove ``hackrf_transfer`` as
a subprocess because gr-osmosdr on this host is built with only ``redpitaya`` and ``file`` sinks and
SoapyHackRF was not installed -- a host-build limitation, correctly documented, not a design choice.
With ``soapysdr-module-hackrf`` present the constraint is gone and the interferer becomes what
everything else is becoming: an authored Companion document that GNU Radio compiles and runs.

WHY THIS IS THE FIRST ONE
-------------------------
It is transmit-only. There is no payload, no grader and no framework terminal to wire, so it
exercises the whole author -> gate -> compile -> execute path on real hardware in isolation, before
any of the measurement machinery exists. Transmit-only is also one of the profiles the toolset needs
anyway: a spectrum logger is receive-only, a beacon is transmit-only, and neither fits a graded
link.

WHAT IT CHANGES SCIENTIFICALLY
------------------------------
A CLI-driven HackRF has no live retune -- the frequency is a launch argument, so the only way to
move it is to kill and relaunch the process. That cost IS the channel-following jammer's reaction
latency, measured through the daemon at 1.77 s. Inside a flowgraph the same move is
``sink.set_frequency(...)`` on a running graph.

That turns a stated limitation into a measurable one. The honest caveat in the hardware results
reads "a lower-latency follower with reaction shorter than the link's dwell wins" -- true, and
previously unprovable here because no such follower could be built. Now it can be, and the hopping
defence can be pushed until it actually fails, which is a stronger result than out-running a jammer
that was slow for tooling reasons.

CONSTRAINTS FOUND ON THIS HARDWARE
----------------------------------
SoapyHackRF accepts only INTEGER-MEGAHERTZ sample rates (1-20 Msps); asking for the link's
2.084 Msps raises. The jammer's rate is therefore decoupled from the link's, which is harmless --
an interferer only has to put energy in the victim's passband -- but it means the two cannot share a
rate constant. Gain is split across two named elements, ``VGA`` (0-47 dB, the strength knob) and
``AMP`` (a 0/14 dB front-end amplifier), rather than the single ``if_gain`` the CLI took.
"""
from __future__ import annotations

#: SoapyHackRF rejects anything else. The link runs at 2.084 Msps; the jammer does not have to
#: match it, and cannot.
DEFAULT_SAMPLE_RATE = 2_000_000

#: Transmit VGA range, in dB. The strength knob.
VGA_RANGE_DB = (0, 47)

#: The front-end amplifier is 0 or 14 dB, nothing between.
AMP_CHOICES_DB = (0, 14)

KINDS = ("cw", "noise")


def jammer_document(center_freq_hz: float, kind: str = "cw", vga_db: int = 20,
                    amp_db: int = 0, tone_offset_hz: float = 100_000.0,
                    bandwidth_hz: float = 1_000_000.0,
                    sample_rate: int = DEFAULT_SAMPLE_RATE,
                    ident: str = "gra_jammer") -> dict:
    """A Companion document for the interferer.

    ``cw`` is a single tone offset from the local oscillator -- the offset keeps it clear of the
    HackRF's own DC/LO leakage spike while staying inside the victim's passband, which is the same
    reason the previous implementation offset its tone. ``noise`` is band-limited Gaussian, for a
    partial-band jammer.

    The document is deliberately plain: a source, a gain scale, and the radio. It is meant to be
    opened in Companion and understood at a glance, because an interferer nobody can inspect is an
    interferer nobody should trust the results of.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown jammer kind {kind!r}; expected one of {', '.join(KINDS)}")
    lo, hi = VGA_RANGE_DB
    if not (lo <= int(vga_db) <= hi):
        raise ValueError(f"vga_db {vga_db} outside the HackRF transmit VGA range {lo}-{hi} dB")
    if int(amp_db) not in AMP_CHOICES_DB:
        raise ValueError(
            f"amp_db {amp_db} is not one of {AMP_CHOICES_DB} — the front-end amplifier is a "
            f"switch, not a continuous gain")

    blocks = []
    if kind == "cw":
        blocks.append({
            "name": "src", "id": "analog_sig_source_x",
            "parameters": {"type": "complex", "samp_rate": str(sample_rate),
                           "waveform": "analog.GR_COS_WAVE", "freq": str(tone_offset_hz),
                           "amp": "1", "offset": "0", "phase": "0"},
            "states": {"coordinate": [120, 160], "state": "enabled"},
        })
    else:
        # Band-limited by generating at the analog noise source and shaping with a low-pass.
        blocks.append({
            "name": "src", "id": "analog_fastnoise_source_x",
            "parameters": {"type": "complex", "noise_type": "analog.GR_GAUSSIAN",
                           "amp": "1", "seed": "0", "samples": "8192"},
            "states": {"coordinate": [120, 160], "state": "enabled"},
        })
        blocks.append({
            "name": "shape", "id": "low_pass_filter",
            "parameters": {"type": "fir_filter_ccf", "decim": "1", "gain": "1",
                           "samp_rate": str(sample_rate),
                           "cutoff_freq": str(bandwidth_hz / 2.0),
                           "width": str(bandwidth_hz / 10.0),
                           "win": "window.WIN_HAMMING", "beta": "6.76"},
            "states": {"coordinate": [300, 160], "state": "enabled"},
        })

    blocks.append({
        "name": "radio", "id": "soapy_hackrf_sink",
        "parameters": {"dev_args": "", "samp_rate": str(sample_rate),
                       "center_freq": str(int(center_freq_hz)),
                       "bandwidth": "0", "amp": str(int(amp_db)), "vga": str(int(vga_db))},
        "states": {"coordinate": [520, 160], "state": "enabled"},
    })

    conns = ([["src", "0", "radio", "0"]] if kind == "cw"
             else [["src", "0", "shape", "0"], ["shape", "0", "radio", "0"]])

    return {
        "metadata": {"file_format": 1, "grc_version": "3.10"},
        "options": {
            "parameters": {
                "id": ident,
                "title": "gr-autopilot interferer",
                "author": "gr-autopilot",
                "description": ("Framework-owned interferer. RESEARCH USE ONLY: "
                                "contained bench only."),
                "generate_options": "no_gui",
                "output_language": "python",
                "run": "True",
            },
            "states": {"coordinate": [8, 8]},
        },
        "blocks": blocks,
        "connections": conns,
    }
