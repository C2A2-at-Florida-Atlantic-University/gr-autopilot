#!/usr/bin/env python3
"""Generate the paper's headline figures from reproducible sim data (+ committed hardware numbers).

Writes to runs/figures/:
  fig1_amc_staircase.{png,pdf} -- (a) the grader's BER per modulation against closed-form theory;
                              (b) the correct spectral-efficiency staircase (exhaustive search).
  fig2_coded_staircase.png -- adaptive modulation AND coding: coded rungs fill the gaps.
  fig3_sample_efficiency.png -- LLM outer loop vs the exhaustive two-loop controller: link runs
                              vs SNR in sim, and total runs / bench wall-clock on the radios.

No radios needed: the BER curves are the numpy AWGN backend (theory-checked); the hardware bars are
the numbers measured on the bench, committed below. Run: python scripts/paper_figures.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.special import erfc

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from gr_autopilot.control.objective import spectral_efficiency  # noqa: E402
from gr_autopilot.link.backend import LinkParams  # noqa: E402
from gr_autopilot.link.numpy_sim import NumpySimBackend  # noqa: E402
from gr_autopilot.scoring.metrics import compute_metrics  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "runs" / "figures"
TARGET = 1e-2
LADDER = ("bpsk", "qpsk", "16qam")
COLORS = {"bpsk": "#38bdf8", "qpsk": "#f5b13d", "16qam": "#fb5a4b",
          "qpsk:conv_k3_r34": "#a78bfa", "16qam:conv_k3_r34": "#f472b6"}
plt.rcParams.update({"font.size": 11, "axes.grid": True, "grid.alpha": 0.3,
                     "figure.facecolor": "white", "axes.axisbelow": True})


def _ber(modcod, es_n0_db, bits, seed=0):
    mod, coding = (modcod.split(":", 1) + [None])[:2] if ":" in modcod else (modcod, None)
    be = NumpySimBackend()
    r = be.run_link(LinkParams(modulation=mod, coding=coding, n_payload_bits=bits,
                               es_n0_db=es_n0_db, seed=seed))
    return compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms).ber


def _optimal(modcods, es_n0_db, bits, target=TARGET):
    meeting = [m for m in modcods if _ber(m, es_n0_db, bits) <= target]
    return max(meeting, key=spectral_efficiency) if meeting else modcods[0]


def _qfunc(x):
    return 0.5 * erfc(x / np.sqrt(2.0))


def _theory_ber(mod, es_n0_db):
    """Exact closed-form Gray-coded BER over AWGN at symbol SNR ``es_n0_db`` (unit-energy
    constellations, as the numpy backend uses). 16-QAM is the exact per-axis 4-PAM expression,
    not the nearest-neighbour approximation, so it holds at low SNR too."""
    g = 10.0 ** (np.asarray(es_n0_db, dtype=float) / 10.0)
    if mod == "bpsk":
        return _qfunc(np.sqrt(2.0 * g))
    if mod == "qpsk":
        return _qfunc(np.sqrt(g))
    if mod == "16qam":
        a = np.sqrt(g / 5.0)
        return 0.25 * (3 * _qfunc(a) + 2 * _qfunc(3 * a) - _qfunc(5 * a))
    raise ValueError(mod)


def fig1_amc_staircase(bits=200_000):
    """Panel (a) checks the grader against theory: dots are the BER the grader counted, dashed
    lines the closed-form BER at the same Es/N0. Panel (b) is the correct policy, computed by
    exhaustive search over the rungs -- the answer key the agent is scored against, not an agent run."""
    snrs = np.arange(2.0, 20.1, 1.0)
    fine = np.linspace(snrs[0], snrs[-1], 400)
    floor = 1.0 / bits
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.2))

    for mod in LADDER:
        axL.semilogy(fine, _theory_ber(mod, fine), "--", color=COLORS[mod], lw=1.2, alpha=0.9)
        bers = np.array([_ber(mod, s, bits) for s in snrs])
        seen = bers > 0          # a trial with no errors has no BER to plot, only a bound
        axL.semilogy(snrs[seen], bers[seen], "o", color=COLORS[mod], markersize=5)
    # Rungs are labelled on their curves, so the legend only has to explain the two marks.
    for mod, x in (("bpsk", 6.0), ("qpsk", 10.0), ("16qam", 15.0)):
        axL.annotate(mod.upper().replace("16QAM", "16-QAM"), (x, _theory_ber(mod, x)),
                     textcoords="offset points", xytext=(7, 3), fontsize=10,
                     color=COLORS[mod], fontweight="bold")
    axL.plot([], [], "o", color="#64748b", markersize=5, label="measured by the grader")
    axL.plot([], [], "--", color="#64748b", lw=1.2, label="closed-form theory")
    axL.axhline(TARGET, ls=":", color="#334155", lw=1)
    axL.text(snrs[0], TARGET * 1.3, f"BER target {TARGET:g}", color="#334155", fontsize=9)
    axL.set_xlabel("Es/N0 (dB)  —  withheld from the agent")
    axL.set_ylabel("BER")
    axL.set_title("(a) grader's BER vs. theory, per rung")
    axL.set_ylim(floor / 2, 1.0)
    axL.legend(loc="lower left", fontsize=9)
    axL.text(0.98, 0.02, f"{bits // 1000}k bits per point;\nerror-free points omitted",
             transform=axL.transAxes, ha="right", va="bottom", fontsize=8, color="#64748b")

    eff = [spectral_efficiency(_optimal(LADDER, s, bits)) for s in snrs]
    axR.step(snrs, eff, where="mid", color="#0f172a", lw=2)
    axR.fill_between(snrs, eff, step="mid", alpha=0.12, color="#38bdf8")
    for y, lbl in [(1, "BPSK"), (2, "QPSK"), (4, "16-QAM")]:
        axR.axhline(y, ls=":", color=COLORS[{1: "bpsk", 2: "qpsk", 4: "16qam"}[y]], lw=1)
        axR.text(snrs[-1] + 0.3, y, lbl, va="center", fontsize=9,
                 color=COLORS[{1: "bpsk", 2: "qpsk", 4: "16qam"}[y]])
    axR.set_xlabel("Es/N0 (dB)")
    axR.set_ylabel("spectral efficiency (bits/symbol)")
    axR.set_title("(b) correct policy: highest rung meeting the target")
    axR.set_yticks([1, 2, 4])
    axR.set_ylim(0, 4.6)
    axR.set_xlim(snrs[0] - 0.5, snrs[-1] + 2.2)

    fig.tight_layout()
    _save(fig, "fig1_amc_staircase.png", dpi=300, close=False)
    _save(fig, "fig1_amc_staircase.pdf")


def fig2_coded_staircase(bits=40_000):
    # Target 1e-3 (as in scripts/coded_amc_sim.py): at 1e-2 the 16-QAM+FEC window is only ~0.5 dB.
    target = 1e-3
    ladder = ("bpsk", "qpsk:conv_k3_r34", "qpsk", "16qam:conv_k3_r34", "16qam")
    snrs = np.arange(4.0, 20.1, 0.5)
    eff = [spectral_efficiency(_optimal(ladder, s, bits, target)) for s in snrs]
    chosen = [_optimal(ladder, s, bits, target) for s in snrs]

    fig, ax = plt.subplots(figsize=(7.6, 4.3))
    ax.step(snrs, eff, where="mid", color="#0f172a", lw=2, zorder=3)
    ax.fill_between(snrs, eff, step="mid", alpha=0.12, color="#a78bfa")
    seen = set()
    for s, mc, e in zip(snrs, chosen, eff):
        if mc not in seen and ":" in mc:  # mark where each coded rung is first chosen
            ax.annotate(mc.replace("16qam", "16-QAM").replace("qpsk", "QPSK").replace(":conv_k3_r34", "+FEC¾"),
                        (s, e), textcoords="offset points", xytext=(6, 6), fontsize=8.5,
                        color="#7c3aed", ha="left", fontweight="bold")
            seen.add(mc)
    ax.set_xlabel("Es/N0 (dB)  —  withheld from the agent")
    ax.set_ylabel("spectral efficiency (bits/symbol)")
    ax.set_title(f"Adaptive modulation AND coding: FEC rungs fill the gaps (BER ≤ {target:g})",
                 fontweight="bold")
    ax.set_yticks([1, 1.5, 2, 3, 4])
    ax.set_ylim(0.6, 4.4)
    fig.tight_layout()
    _save(fig, "fig2_coded_staircase.png")


def fig3_sample_efficiency(bits=200_000):
    # (a) sim: link runs vs SNR — the exhaustive climb is flat, the guided policy adapts.
    from scripts.llm_ablation import run_guided, run_two_loop
    snrs = [5, 7, 9, 11, 13, 15, 17, 19]
    tl = [run_two_loop(s, TARGET, bits, 12, 0)[1] for s in snrs]
    gd = [run_guided(s, 0.0, TARGET, bits, 12, 0)[1] for s in snrs]

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.2))
    axL.plot(snrs, tl, "o-", color="#fb5a4b", label="two-loop (exhaustive climb)")
    axL.plot(snrs, gd, "s-", color="#38bdf8", label="LLM-guided (top-down)")
    axL.set_xlabel("Es/N0 (dB)")
    axL.set_ylabel("link runs to converge")
    axL.set_title("(a) simulation — same accuracy, far fewer trials")
    axL.legend()

    # (b) hardware: total runs and bench wall-clock, as measured on the coaxial bench.
    labels = ["link runs", "wall-clock (s)"]
    climb = [117, 139.0]
    guided = [17, 10.7]
    x = np.arange(len(labels))
    w = 0.36
    b1 = axR.bar(x - w / 2, climb, w, color="#fb5a4b", label="two-loop climb")
    b2 = axR.bar(x + w / 2, guided, w, color="#38bdf8", label="LLM-guided")
    axR.set_xticks(x, labels)
    axR.set_title("(b) on the radios — 6.9× fewer runs, 13× faster")
    axR.set_ylim(0, 165)
    axR.legend(loc="upper center")
    for bars in (b1, b2):
        for bar in bars:
            axR.annotate(f"{bar.get_height():g}", (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                         textcoords="offset points", xytext=(0, 3), ha="center", fontsize=9)

    fig.suptitle("Sample efficiency: LLM outer loop vs the exhaustive two-loop controller",
                 fontweight="bold")
    fig.tight_layout()
    _save(fig, "fig3_sample_efficiency.png")


def fig4_hopping_evasion(bits=40_000):
    from gr_autopilot.link.hopping import HopPlan, jammed_fraction
    from gr_autopilot.link.interference import Interferer
    from gr_autopilot.link.interference_sim import InterferenceSimBackend
    chans = tuple(2.400e9 + k * 1e6 for k in range(8))
    rates = np.array([25, 50, 75, 100, 150, 200, 300, 400, 600, 800, 1200, 1600])
    cases = [(10e-3, "#fb5a4b"), (5e-3, "#f5b13d"), (2e-3, "#38bdf8")]  # reaction tau: slow -> fast

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.2))
    for tau, col in cases:
        jam = Interferer(center_freq_hz=2.4e9, inr_db=30.0, reactive=True, reaction_s=tau)
        lbl = f"τ={tau*1e3:g} ms (evade > {1/tau:.0f} Hz)"
        axL.plot(rates, [jammed_fraction(jam, HopPlan(chans, r), 350e3) for r in rates],
                 "o-", color=col, ms=3, label=lbl)
        be = InterferenceSimBackend(clean_es_n0_db=20.0, interferer=jam, center_freq_hz=2.4e9)
        bers = []
        for r in rates:
            be.set_condition(hop_plan=HopPlan(chans, r))
            res = be.run_link(LinkParams(modulation="qpsk", n_payload_bits=bits, sps=8, rolloff=0.35, seed=1))
            bers.append(max(compute_metrics(res.tx_bits, res.rx_bits, res.rx_syms, res.ref_syms).ber, 1e-5))
        axR.semilogy(rates, bers, "s-", color=col, ms=3, label=f"τ={tau*1e3:g} ms")

    axL.set_xlabel("hop rate (Hz)")
    axL.set_ylabel("jammed fraction")
    axL.set_title("(a) reactive jammer out-hopped above 1/τ")
    axL.legend(fontsize=9)
    axR.axhline(1e-2, ls="--", color="#334155", lw=1)
    axR.text(rates[0], 1.3e-2, "BER target 1e-2", color="#334155", fontsize=9)
    axR.set_xlabel("hop rate (Hz)")
    axR.set_ylabel("QPSK BER")
    axR.set_title("(b) BER collapses once hopping wins")
    axR.set_ylim(5e-6, 1.0)
    axR.legend(fontsize=9)
    fig.suptitle("Stage 3 — frequency hopping evades a reactive jammer (hop faster than 1/τ)",
                 fontweight="bold")
    fig.tight_layout()
    _save(fig, "fig4_hopping_evasion.png")


def fig5_hw_hopping():
    """Stage-3 on hardware: the MEASURED jammed fraction and aggregate BER across the four scenarios
    on the real Plutos + HackRF. No radios to plot: these are the committed bench numbers, as fig3(b)
    plots the committed ablation numbers."""
    labels = ["camp\n(no hop)", "fixed jammer\n+ hop (1/N)", "follower\nout-hopped",
              "follower keeps up\n(1/τ floor)"]
    jam_frac = [1.00, 0.25, 0.00, 1.00]
    agg_ber = [4.82e-1, 1.22e-1, 4.5e-4, 4.84e-1]
    good = "#22c55e"
    cols = ["#fb5a4b", "#f5b13d", good, "#fb5a4b"]   # evasion (green) vs jammed (red/amber)
    x = np.arange(len(labels))

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.3))
    b1 = axL.bar(x, jam_frac, 0.62, color=cols)
    axL.set_xticks(x, labels, fontsize=9)
    axL.set_ylabel("jammed fraction (measured)")
    axL.set_ylim(0, 1.12)
    axL.set_title("(a) fraction of hops the jammer actually hit")
    for bar, v in zip(b1, jam_frac):
        axL.annotate(f"{v:.2f}", (bar.get_x() + bar.get_width() / 2, v),
                     textcoords="offset points", xytext=(0, 3), ha="center", fontsize=10,
                     fontweight="bold")

    b2 = axR.bar(x, agg_ber, 0.62, color=cols)
    axR.set_yscale("log")
    axR.set_xticks(x, labels, fontsize=9)
    axR.set_ylabel("aggregate BER over the hop cycle")
    axR.set_ylim(1e-4, 1.5)
    axR.axhline(1e-2, ls="--", color="#334155", lw=1)
    axR.text(-0.4, 1.3e-2, "BER target 1e-2", color="#334155", fontsize=9)
    axR.set_title("(b) hopped-link BER: evaded vs jammed")
    for bar, v in zip(b2, agg_ber):
        axR.annotate(f"{v:.1e}", (bar.get_x() + bar.get_width() / 2, v),
                     textcoords="offset points", xytext=(0, 3), ha="center", fontsize=9)

    fig.suptitle("Stage 3 on the radios — out-hop a channel-following jammer "
                 "(Pluto live ~4 ms retune vs HackRF-CLI ~1.2 s → ~4× faster)", fontweight="bold")
    fig.tight_layout()
    _save(fig, "fig5_hw_hopping.png")


def fig6_soft_decision(bits=120_000):
    """Soft-decision (LLR) Viterbi vs hard-decision: the ~2 dB coding gain, on the theory-checked
    numpy AWGN backend. Coded QPSK, rate-1/2 K=3 convolutional code."""
    be = NumpySimBackend()

    def ber(es, soft):
        r = be.run_link(LinkParams(modulation="qpsk", coding="conv_k3_r12", n_payload_bits=bits,
                                   es_n0_db=es, soft_decision=soft, seed=3))
        return max(compute_metrics(r.tx_bits, r.rx_bits, r.rx_syms, r.ref_syms).ber, 5e-6)

    snrs = np.arange(1.0, 8.01, 0.5)
    hard = [ber(s, False) for s in snrs]
    soft = [ber(s, True) for s in snrs]

    def cross(bers, target=1e-3):  # Es/N0 where a curve hits `target`, by interp on -log10(BER)
        y = -np.log10(np.array(bers))
        return float(np.interp(-np.log10(target), y, snrs))

    fig, ax = plt.subplots(figsize=(7.4, 4.3))
    ax.semilogy(snrs, hard, "o-", color="#fb5a4b", label="hard-decision Viterbi", markersize=4)
    ax.semilogy(snrs, soft, "s-", color="#38bdf8", label="soft-decision (LLR) Viterbi", markersize=4)
    xh, xs = cross(hard), cross(soft)
    ax.axhline(1e-3, ls="--", color="#334155", lw=1)
    ax.annotate("", xy=(xs, 1e-3), xytext=(xh, 1e-3),
                arrowprops=dict(arrowstyle="<->", color="#0f172a", lw=1.5))
    ax.text((xh + xs) / 2, 1.4e-3, f"~{xh - xs:.1f} dB", ha="center", fontsize=10, fontweight="bold")
    ax.set_xlabel("Es/N0 (dB)")
    ax.set_ylabel("info BER — coded QPSK (rate-1/2 K=3)")
    ax.set_title("Soft-decision Viterbi: ~2 dB coding gain from demodulator LLRs", fontweight="bold")
    ax.set_ylim(5e-6, 1.0)
    ax.legend()
    fig.tight_layout()
    _save(fig, "fig6_soft_decision.png")


def fig7_band_envelope():
    """Stage-1 sync robustness ACROSS RF bands: the ZC acquisition front-end tolerates a fixed CFO
    range in Hz, but the two-Pluto LO offset is ppm·f_c, so there is a band ceiling above which
    acquisition fails independent of SNR. (a) CFO vs band per ppm with the acquisition-range line —
    the crossover is the ceiling; (b) measured acquisition rate collapsing at that same knee. Pure
    numpy over the real sync.acquire (scripts/band_sweep_sim.py); the wired-hardware sweep confirms it."""
    from gr_autopilot.link import band
    Rs = band.symbol_rate_hz()
    rng_khz = band.acquisition_range_hz(Rs) / 1e3
    ppms = [(10.0, "#38bdf8"), (25.0, "#f5b13d"), (40.0, "#fb5a4b")]
    bands = np.array([0.4, 0.7, 0.9, 1.2, 1.5, 1.8, 2.1, 2.4, 3.0, 3.5])

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.2))
    fine = np.linspace(0.2, 4.0, 60)
    for ppm, col in ppms:
        axL.plot(fine, [band.cfo_hz(f * 1e9, ppm) / 1e3 for f in fine], color=col, lw=1.8,
                 label=f"{ppm:g} ppm  (ceiling {band.band_ceiling_hz(ppm, Rs)/1e9:.2f} GHz)")
        ceil = band.band_ceiling_hz(ppm, Rs) / 1e9
        if ceil <= 4.0:
            axL.plot([ceil], [rng_khz], "o", color=col, ms=7, mfc="white", mew=1.8)
    # Overlay the WIRED-hardware measurement (docs/data/hw_band_sweep.json): the bench pair is
    # well-disciplined (~0.3 ppm), so its CFO hugs the bottom — deep in the safe zone across all bands.
    hw = Path(__file__).resolve().parents[1] / "docs" / "data" / "hw_band_sweep.json"
    if hw.exists():
        import json
        d = json.loads(hw.read_text())
        pts = [(r["center_freq_hz"] / 1e9, r["cfo_hz"] / 1e3) for r in d.get("rows", [])
               if r.get("locked") and r.get("cfo_hz") is not None]
        if pts:
            ppm_fit = d.get("fitted_ppm", 0.3)
            axL.plot([p[0] for p in pts], [p[1] for p in pts], "D", color="#22c55e", ms=5,
                     label=f"measured (wired Plutos, {ppm_fit:.1f} ppm)")
    axL.axhline(rng_khz, ls="--", color="#334155", lw=1.2)
    axL.text(0.25, rng_khz * 1.06, f"acquisition range  ±{rng_khz:.0f} kHz (cfo_max·Rs)",
             color="#334155", fontsize=9)
    axL.set_xlabel("band — link center frequency (GHz)")
    axL.set_ylabel("differential CFO (kHz)")
    axL.set_title("(a) CFO = ppm·f_c crosses the range → ceiling")
    axL.set_ylim(0, 160)
    axL.legend(fontsize=8.5, loc="upper left")

    for ppm, col in ppms:
        rows = band.band_sweep(bands * 1e9, ppm, trials=16, es_n0_db=15.0)
        axR.plot(bands, [100 * r["acq_rate"] for r in rows], "s-", color=col, ms=4, label=f"{ppm:g} ppm")
        ceil = band.band_ceiling_hz(ppm, Rs) / 1e9
        if ceil <= 3.6:
            axR.axvline(ceil, ls=":", color=col, lw=1, alpha=0.7)
    axR.set_xlabel("band — link center frequency (GHz)")
    axR.set_ylabel("correct-acquisition rate (%)")
    axR.set_title("(b) acquisition collapses at that knee (Es/N0 15 dB)")
    axR.set_ylim(-5, 108)
    axR.legend(fontsize=9)
    fig.suptitle("Band envelope — ZC acquisition is CFO-limited across bands, not SNR-limited",
                 fontweight="bold")
    fig.tight_layout()
    _save(fig, "fig7_band_envelope.png")


def _save(fig, name, dpi=140, close=True):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    if close:
        plt.close(fig)
    print(f"wrote {path.relative_to(OUT.parents[1])}")


def main() -> int:
    fig1_amc_staircase()
    fig2_coded_staircase()
    fig3_sample_efficiency()
    fig4_hopping_evasion()
    fig5_hw_hopping()
    fig6_soft_decision()
    fig7_band_envelope()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
