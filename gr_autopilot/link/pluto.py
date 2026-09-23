"""PlutoSDR hardware link backend (gr-iio).

Runs one real over-the-air (over-the-cable, in the lab) trial across two PlutoSDRs:

    known frame [sync preamble | payload]  --RRC pulse-shape (circular, periodic)-->
        gr-iio fmcomms2_sink (TX Pluto)  ==RF path==>  gr-iio fmcomms2_source (RX Pluto)
    --> matched filter --> Gardner symbol timing recovery --> Costas carrier recovery
    --> preamble frame-sync --> payload symbols --> (framework grades BER/EVM/SNR)

Design notes
------------
* **Transport**: gr-iio in-process, TX driven by a ``vector_source(repeat=True)`` streamed
  continuously into the sink. This avoids the ``iio_writedev -c`` cyclic-DMA truncation that
  silently shortens the transmitted period (measured: a 32768-sample buffer repeated at
  ~16020 samples over the CLI path; gr-iio repeats it faithfully at 32768).
* **Tracking receiver**: two free-running Pluto oscillators drift, so offline block-wise
  correction cannot lock. Gardner (non-data-aided) timing tolerant to residual CFO, then a
  Costas loop for carrier phase/frequency. Timing loop bandwidth ~2*pi*0.02 is inside the
  stable range (2*pi*0.03+ slips symbols on this hardware).
* **Frame sync**: a framework-owned high-autocorrelation sync preamble. The agent's receiver
  is blind (Gardner/Costas use no payload knowledge); the preamble is protocol overhead known
  to both ends. Final 1:1 payload alignment for *scoring* is data-aided and framework-owned.
* **SNR is physical and hidden**: the operator/framework sets a fixed link budget via
  ``tx_atten_db`` / ``rx_gain_db``; ``params.es_n0_db`` / ``params.noise_voltage`` (sim knobs)
  are ignored. The realized SNR is measured and returned in ``meta`` (the grader sees it; the
  agent does not).

GNU Radio / gr-iio are imported lazily inside ``run_link`` so importing this module needs
neither installed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from gr_autopilot import modulation
from gr_autopilot.coding import CODING_RATES, coding_decode, coding_encode, info_bits_for_budget
from gr_autopilot.link import sync
from gr_autopilot.link.backend import LinkBackend, LinkParams, LinkResult
from gr_autopilot.scoring.payload import known_payload

# Framework-owned sync preamble: a fixed, high-autocorrelation sequence known to both ends.
# Seed chosen for a sharp aperiodic frame-sync peak (a small-seed PRBS warms up into a
# near-constant run, which is useless for frame sync — this seed fills the register).
_PREAMBLE_SEED = 0x6B2F
# Base constellation used for the preamble, per payload modulation's rotational symmetry:
# BPSK payload (180deg ambiguity) needs a BPSK preamble; QPSK/16-QAM (90deg) need QPSK.
# Carrier recovery per modulation. Constant-modulus (BPSK/QPSK/8-PSK) use a Costas loop of the
# given order. QAM is NOT constant-modulus, so a Costas loop's error signal is inconsistent
# across inner/outer points and spins the constellation into a ring (measured BER ~0.37 on
# 16-QAM); instead QAM uses timing-only recovery + a decision-directed PLL seeded by the preamble
# phase (16-QAM measured BER ~1.6e-3), or the ZC/PSAM path. ``None`` selects the DD-PLL path.
# The preamble's base constellation matches the payload's rotational ambiguity: 8-PSK has a
# 45-degree ambiguity, so its preamble is 8-PSK; every QAM has 90 degrees, so QPSK serves.
#
# Whether 32/64-QAM actually close on this bench is a MEASUREMENT (the error-vector floor at the
# chosen link budget), not something this table asserts; 256-QAM is listed for completeness and
# is not expected to work on free-running oscillators.
_PREAMBLE_BASE = {"bpsk": "bpsk", "qpsk": "qpsk", "8psk": "8psk", "16qam": "qpsk",
                  "32qam": "qpsk", "64qam": "qpsk", "256qam": "qpsk"}
_COSTAS_ORDER = {"bpsk": 2, "qpsk": 4, "8psk": 8, "16qam": None, "32qam": None,
                 "64qam": None, "256qam": None}


def sync_preamble(payload_mod: str, n_syms: int) -> np.ndarray:
    """Deterministic framework-owned frame-sync preamble for a given payload modulation."""
    base = _PREAMBLE_BASE[payload_mod]
    const = modulation.get(base)
    bits = known_payload(n_syms * const.bits_per_symbol, order=15, seed=_PREAMBLE_SEED)
    return const.modulate(bits)


@dataclass
class _Condition:
    """The operator-set physical link budget (hidden from the agent)."""

    center_freq_hz: float = 2_400_000_000.0
    tx_atten_db: float = 10.0     # Pluto TX attenuation (0 = max power)
    rx_gain_db: float = 40.0      # Pluto RX manual gain


class PlutoBackend(LinkBackend):
    """Hardware LinkBackend over two PlutoSDRs via gr-iio."""

    name = "pluto"
    owns_channel = True  # the hidden SNR is physical (TX/RX gains), set via set_condition

    def __init__(
        self,
        tx_uri: str,
        rx_uri: str,
        *,
        center_freq_hz: float = 2_400_000_000.0,
        sample_rate: float = 2_084_000.0,
        tx_atten_db: float = 10.0,
        rx_gain_db: float = 40.0,
        preamble_len: int = 256,
        ntaps_per_symbol: int = 8,
        buffer_size: int = 0x8000,
        capture_periods: int = 4,
        timing_loop_bw: float = 2 * np.pi * 0.02,
        carrier_loop_bw: float = 2 * np.pi * 0.02,
        framesync_blocks: int = 8,
        dd_mu1: float = 0.05,
        dd_mu2: float = 0.001,
        tx_scale: float = 0.5,
        settle_s: float = 0.3,
        rx_gain_mode: str = "manual",
        sync_mode: str = "legacy",
        zc_half: int = 64,
        zc_root: int = 25,
        zc_cfo_max: float = 0.15,
        pilot_spacing: int = 0,
    ):
        self.tx_uri = tx_uri
        self.rx_uri = rx_uri
        self.sample_rate = float(sample_rate)
        self.preamble_len = int(preamble_len)
        # Frame-sync front-end: "legacy" = blind Gardner+Costas then correlate (the original,
        # validated at good SNR); "zc" = data-aided Zadoff-Chu acquisition first (see link/sync.py),
        # which lowers the acquisition floor below the blind loops' ~11 dB lock limit. The two use
        # different preambles, but both are framework-owned overhead invisible to the agent.
        self.sync_mode = str(sync_mode)
        self.zc_half = int(zc_half)
        self.zc_root = int(zc_root)
        self.zc_cfo_max = float(zc_cfo_max)
        # PSAM: with pilot_spacing > 0, interleave a known pilot every N payload symbols and recover
        # the carrier by interpolating the pilot phases (rotation-invariant) instead of the blind
        # decision-directed loop — the fix for the 90° slip that breaks coded 16-QAM at marginal SNR
        # measured on the bench. 0 = off (blind DD loop). ZC path only.
        self.pilot_spacing = int(pilot_spacing)
        self.ntaps_per_symbol = int(ntaps_per_symbol)
        self.buffer_size = int(buffer_size)
        self.capture_periods = int(capture_periods)
        self.timing_loop_bw = float(timing_loop_bw)
        self.carrier_loop_bw = float(carrier_loop_bw)
        # Sub-blocks for the 16-QAM frame-sync candidate search (see _framesync_dd). The search
        # correlates each sub-block coherently and sums magnitudes, so it tolerates ~1 cycle of
        # CFO per sub-block instead of ~1 cycle across the whole preamble. 1 = the old fully
        # coherent behaviour.
        self.framesync_blocks = int(framesync_blocks)
        self.dd_mu1 = float(dd_mu1)
        self.dd_mu2 = float(dd_mu2)
        self.tx_scale = float(tx_scale)
        self.settle_s = float(settle_s)
        # "manual" (fixed rx_gain_db) or an AGC mode ("slow_attack"/"fast_attack"). AGC keeps the
        # signal in the ADC's linear range so the measured SNR reflects the analog chain rather
        # than ADC quantization at high path loss -- the physically-correct way to sweep SNR by
        # attenuation alone.
        self.rx_gain_mode = str(rx_gain_mode)
        self.condition = _Condition(center_freq_hz, tx_atten_db, rx_gain_db)
        # Persistent, always-running gr-iio flowgraph (built lazily). gr-iio device contexts
        # hang on destruction, so we build/start ONCE and reuse across trials, updating the TX
        # waveform live via lock()/set_data()/unlock() rather than rebuilding per trial.
        self._tb = None
        self._txsrc = self._txsink = self._rxsrc = self._rxsnk = None
        self._running_cfg = None

    # -- operator/framework controls (the hidden channel condition) --
    #: What the radio can actually be told (see LinkBackend.applies_condition). A hop plan and a
    #: relative tx_power_db are NOT here: this backend has no live hop scheduler, and its transmit
    #: level is the operator's tx_atten_db, not an agent-relative power.
    applies_condition = frozenset({"center_freq_hz", "tx_atten_db", "rx_gain_db"})

    def set_condition(self, **cond) -> None:
        """Apply the hidden physical condition. Accepts center_freq_hz / tx_atten_db /
        rx_gain_db; unknown keys are ignored so a sim-style channel dict can't crash it.

        Tolerance here is safe only because ``applies_condition`` above lets the service refuse a
        knob this backend cannot honour BEFORE it is dropped on the floor."""
        if cond.get("center_freq_hz") is not None:
            self.condition.center_freq_hz = float(cond["center_freq_hz"])
        if cond.get("tx_atten_db") is not None:
            self.condition.tx_atten_db = float(cond["tx_atten_db"])
        if cond.get("rx_gain_db") is not None:
            self.condition.rx_gain_db = float(cond["rx_gain_db"])

    # -- one trial --
    def run_link(self, params: LinkParams) -> LinkResult:
        from gnuradio.filter import firdes

        const = modulation.get(params.modulation)
        bps = const.bits_per_symbol
        sps = int(params.sps)
        if sps < 2 or sps % 2 != 0:
            raise ValueError(f"PlutoBackend needs an even sps >= 2, got {sps}")

        # Known payload -> symbols. Optionally FEC-coded: the info payload is convolutionally
        # encoded to fill the same symbol budget; the frame alignment works on the coded symbols
        # and BER is graded on the (post-Viterbi) info bits. The ZC front-end is
        # modulation-independent and returns the exact payload boundary, so it carries coding for
        # every rung including 16-QAM; the legacy Costas path (BPSK/QPSK) does too, but has no
        # 16-QAM carrier recovery, so coded 16-QAM needs sync_mode="zc".
        n_syms = params.n_payload_bits // bps
        coding = params.coding
        info_bits = coded_len = n_info = None
        if coding is not None:
            if coding not in CODING_RATES:
                raise ValueError(f"unknown coding {coding!r}")
            if _COSTAS_ORDER[params.modulation] is None and self.sync_mode != "zc":
                raise NotImplementedError(
                    f"coded {params.modulation} needs the ZC front-end (sync_mode='zc'); the "
                    "legacy Costas path has no QAM carrier recovery")
            n_info = info_bits_for_budget(n_syms * bps, coding)
            info_bits = known_payload(n_info, order=params.prbs_order, seed=params.seed)
            coded_bits = coding_encode(info_bits, coding)
            coded_len = coded_bits.size
            pad = (-coded_len) % bps
            payload_bits = np.concatenate([coded_bits, np.zeros(pad, dtype=coded_bits.dtype)])
            tx_bits = payload_bits            # the "known payload" for frame alignment is coded
        else:
            tx_bits = known_payload(n_syms * bps, order=params.prbs_order, seed=params.seed)
        tx_syms = const.modulate(tx_bits)

        # Frame = preamble + payload, pulse-shaped by CIRCULAR convolution so the repeated
        # waveform is glitch-free and truly periodic. The ZC preamble is a constant-modulus
        # [z|z] (modulation-independent); the legacy one is a per-modulation PRBS sequence.
        if self.sync_mode == "zc":
            pre = sync.zc_preamble(self.zc_half, self.zc_root)
        else:
            pre = sync_preamble(params.modulation, self.preamble_len)
        # PSAM (ZC path only): interleave a known pilot every N payload symbols. The pilot is a
        # CORNER constellation point -- a valid symbol (so the DD loop decides it correctly and is not
        # perturbed) and the strongest point (best phase estimate), with four distinct 90° images.
        use_pilots = self.sync_mode == "zc" and self.pilot_spacing > 0
        if use_pilots:
            pilot_sym = complex(const.points[int(np.argmax(np.abs(const.points)))])
            payload_syms, is_pilot = sync.psam_insert(tx_syms, self.pilot_spacing, pilot_sym)
        else:
            pilot_sym, payload_syms, is_pilot = None, tx_syms, None
        frame = np.concatenate([pre, payload_syms])
        n_frame = frame.size
        up = np.zeros(n_frame * sps, dtype=complex)
        up[::sps] = frame
        ntaps = self.ntaps_per_symbol * sps + 1
        rrc = np.asarray(firdes.root_raised_cosine(1.0, float(sps), 1.0, params.rolloff, ntaps))
        wave = np.fft.ifft(np.fft.fft(up) * np.fft.fft(rrc, up.size))
        peak = np.max(np.abs(wave))
        wave = (wave / peak * self.tx_scale).astype(np.complex64)

        n_capture = max(self.capture_periods, 3) * n_frame * sps
        rx = self._transmit_capture(wave, n_capture)

        # Decode with the tracking receiver + frame sync.
        if self.sync_mode == "zc":
            rx_syms, tx_syms_al, tx_bits_al, ref_syms, rx_bits, dbg = self._decode_zc(
                rx, const, tx_bits, tx_syms, pre, sps, params.rolloff,
                pilots=(payload_syms.size, is_pilot, pilot_sym) if use_pilots else None,
            )
        else:
            rx_syms, tx_syms_al, tx_bits_al, ref_syms, rx_bits, dbg = self._decode(
                rx, const, tx_bits, tx_syms, pre, sps, params.rolloff,
                _COSTAS_ORDER[params.modulation],
            )

        if coding is not None:
            # rx_bits are the recovered CODED bits (aligned to the full coded payload). Viterbi
            # them back to info bits; BER is graded on info, EVM/SNR on the coded symbols. NOTE: at
            # marginal SNR the blind decision-directed carrier loop can slip to a 90°-rotated alias
            # of the (rotationally symmetric) 16-QAM constellation, permuting every symbol's bits so
            # Viterbi decodes garbage. The ZC preamble phase anchor (above) removes this at good SNR;
            # robust operation at the marginal SNRs where coding matters needs rotation-invariant
            # carrier recovery (pilots / differential); PSAM below is that path.
            rx_coded = np.asarray(rx_bits)[:coded_len]
            if rx_coded.size < coded_len:  # short capture -> pad (decodes with tail errors, honest)
                rx_coded = np.concatenate([rx_coded, np.zeros(coded_len - rx_coded.size, np.int8)])
            out_rx_bits = coding_decode(rx_coded, coding, n_info)
            out_tx_bits = info_bits[:out_rx_bits.size]
        else:
            out_tx_bits, out_rx_bits = tx_bits_al, rx_bits

        meta = {
            "backend": self.name,
            "tx_uri": self.tx_uri,
            "rx_uri": self.rx_uri,
            "center_freq_hz": self.condition.center_freq_hz,
            "sample_rate": self.sample_rate,
            "symbol_rate": self.sample_rate / sps,
            "tx_atten_db": self.condition.tx_atten_db,
            "rx_gain_mode": self.rx_gain_mode,
            # Only assert a gain value when we actually set it. Under AGC the hardware picks the gain
            # per-capture, so reporting the (un-applied) constructor rx_gain_db would corrupt the
            # link-budget provenance a reviewer reconstructs — record None + the mode instead.
            "rx_gain_db": (self.condition.rx_gain_db if self.rx_gain_mode == "manual" else None),
            "sps": sps,
            "rolloff": params.rolloff,
            "preamble_len": self.preamble_len,
            "n_syms": n_syms,
            "n_syms_used": int(tx_syms_al.size),
            "coding": coding,
            "n_info": n_info,
            **dbg,
        }
        return LinkResult(
            modulation=const.name,
            tx_bits=out_tx_bits,
            rx_bits=out_rx_bits,
            tx_syms=tx_syms_al,
            rx_syms=rx_syms,
            ref_syms=ref_syms,
            meta=meta,
        )

    # -- persistent gr-iio flowgraph: TX(repeat) + RX, built/started once --
    def _ensure_running(self) -> None:
        rate = int(self.sample_rate)
        cfg = rate  # persistent transport keyed on sample rate ONLY; center freq is retuned live
        if self._tb is not None and self._running_cfg == cfg:
            return
        if self._tb is not None:  # sample rate changed -> rebuild (rare)
            self.close()
        from gnuradio import blocks, gr, iio

        c = self.condition
        tb = gr.top_block()
        txsrc = blocks.vector_source_c(np.zeros(self.buffer_size, np.complex64).tolist(), True)
        txsink = iio.fmcomms2_sink_fc32(self.tx_uri, [True, True], self.buffer_size, False)
        txsink.set_frequency(int(c.center_freq_hz))
        txsink.set_samplerate(rate)
        txsink.set_bandwidth(int(rate * 1.2))
        txsink.set_attenuation(0, float(c.tx_atten_db))
        rxsrc = iio.fmcomms2_source_fc32(self.rx_uri, [True, True], self.buffer_size)
        rxsrc.set_frequency(int(c.center_freq_hz))
        rxsrc.set_samplerate(rate)
        if hasattr(rxsrc, "set_bandwidth"):     # match the TX analog BW where the source block exposes
            rxsrc.set_bandwidth(int(rate * 1.2))  # it — some gr-iio builds only put set_bandwidth on the sink
        rxsrc.set_gain_mode(0, self.rx_gain_mode)
        if self.rx_gain_mode == "manual":
            rxsrc.set_gain(0, float(c.rx_gain_db))
        rxsnk = blocks.vector_sink_c()
        # A capture GATE, closed except inside a measurement window.
        #
        # Without it this leaks the machine. `vector_sink` accumulates every sample it is ever
        # handed, and this flowgraph is persistent by necessity (gr-iio contexts hang on
        # destruction), so the receiver feeds it at the full sample rate -- ~16.7 MB/s at
        # 2.084 Msps -- for the whole time an agent spends thinking between trials. Measured: the
        # worker reached 11 GB resident in under two hours of ordinary use, took the machine into
        # swap, and killed an unrelated process. Resetting around each capture does not help,
        # because the idle gaps between captures are where the samples pile up.
        #
        # `blocks.copy` disabled drops its input instead of forwarding it, so the graph keeps
        # running -- the radios stay open and the loops stay converged -- while nothing reaches the
        # sink. The same object is the right home for a transmit-idle policy.
        rxgate = blocks.copy(gr.sizeof_gr_complex)
        rxgate.set_enabled(False)
        tb.connect(txsrc, txsink)
        tb.connect(rxsrc, rxgate, rxsnk)
        tb.start()
        self._tb, self._txsrc, self._txsink, self._rxsrc, self._rxsnk = (
            tb, txsrc, txsink, rxsrc, rxsnk
        )
        self._rxgate = rxgate
        self._running_cfg = cfg

    def _gated_capture(self, n_capture: int) -> np.ndarray:
        """Open the capture gate, take exactly what is needed, close it again.

        The gate is what keeps the receiver from accumulating between trials (see the note where it
        is built). It is closed in a finally block: an exception here must not leave the sink
        filling at 16.7 MB/s for the rest of the session.

        ``vector_sink.data()`` copies the whole accumulated buffer on every call, so polling it in a
        loop is quadratic in the capture length. With the gate closed between windows the buffer
        stays bounded by ``n_capture``, which is what makes the poll affordable.
        """
        rate = self._running_cfg
        self._rxsnk.reset()
        self._rxgate.set_enabled(True)
        try:
            deadline, waited = n_capture / rate + 5.0, 0.0
            while len(self._rxsnk.data()) < n_capture and waited < deadline:
                time.sleep(0.02)
                waited += 0.02
            rx = np.asarray(self._rxsnk.data(), dtype=np.complex128)[:n_capture]
        finally:
            self._rxgate.set_enabled(False)
            self._rxsnk.reset()
        if rx.size < n_capture:
            raise RuntimeError(f"PlutoBackend captured {rx.size}/{n_capture} samples")
        return rx - rx.mean()

    def _transmit_capture(self, wave: np.ndarray, n_capture: int) -> np.ndarray:
        """Update the live TX waveform, then grab ``n_capture`` fresh RX samples from the
        continuously-running flowgraph."""
        self._ensure_running()
        c = self.condition
        # Apply the current condition LIVE on the running flowgraph (no rebuild): retune both LOs
        # and set gains. Live retuning is what makes frequency-avoidance experiments possible --
        # rebuilding to change center frequency would hit the gr-iio context-teardown hang.
        self._txsink.set_frequency(int(c.center_freq_hz))
        self._rxsrc.set_frequency(int(c.center_freq_hz))
        self._txsink.set_attenuation(0, float(c.tx_atten_db))  # apply current SNR condition live
        if self.rx_gain_mode == "manual":
            self._rxsrc.set_gain(0, float(c.rx_gain_db))
        self._tb.lock()
        self._txsrc.set_data(wave.tolist(), [])
        self._tb.unlock()
        time.sleep(self.settle_s)  # let the new waveform reach the air / flush old DMA

        return self._gated_capture(n_capture)

    def _silent_capture(self, freq_hz: float, n_capture: int) -> np.ndarray:
        """Retune both LOs to ``freq_hz``, drive the link TX with zeros (silent), and grab fresh
        RX samples -- so the capture holds only the interferer + noise."""
        self._ensure_running()
        self._txsink.set_frequency(int(freq_hz))
        self._rxsrc.set_frequency(int(freq_hz))
        if self.rx_gain_mode == "manual":
            self._rxsrc.set_gain(0, float(self.condition.rx_gain_db))
        self._tb.lock()
        self._txsrc.set_data(np.zeros(self.buffer_size, np.complex64).tolist(), [])
        self._tb.unlock()
        time.sleep(self.settle_s)
        try:
            return self._gated_capture(n_capture)
        except RuntimeError:
            # A short silent capture is a weak reading, not a failed trial: sensing runs while the
            # link is quiet, so there is nothing driving the receiver to a full buffer.
            return np.zeros(0, dtype=np.complex128)

    def sense_spectrum(self, freqs_hz, bw_hz=None, margin_db: float = 6.0,
                       peak_margin_db: float = 10.0):
        """Monitor role (spec §6): energy-detect received power in the link band at each candidate
        frequency, link silent. For each ``f`` the RX tunes to ``f``, captures, and measures the PSD
        over ``+-bw/2`` around DC (the band a link at ``f`` would actually occupy).

        TWO statistics, because one of them cannot see the jammer this framework ships:

        * ``power_db`` -- mean in-band power over a median noise floor. Sensitive to WIDEBAND
          occupancy (someone's WiFi), which spreads energy across the whole band.
        * ``peak_db`` -- the strongest bin over the in-band median bin. Sensitive to NARROWBAND
          energy: a continuous-wave tone.

        The mean alone is structurally blind to a tone. At 2.084 Msps a 400 kHz window spans ~786
        FFT bins, so a single-bin carrier is averaged down by about 29 dB -- measured on the bench,
        a CW jammer that drove the link to a bit error ratio of 0.41 registered 5.3 dB of mean
        power and was reported CLEAR against a 6 dB threshold. An agent told the channel was clear
        while its link was being destroyed has no way to reason its way out of that.

        A channel is occupied if EITHER statistic exceeds its margin. Both are measured against the
        MEDIAN CANDIDATE, so a candidate set in which every channel is equally occupied reads as
        uniformly clear -- scan more candidates than you expect to be jammed. (A *reactive* jammer
        stays idle here -- it only fires when the link transmits -- so no energy detector sees it;
        that is the escalation the agent is meant to infer from sensing clear yet still failing.)
        """
        if len(list(freqs_hz)) < 2:
            # Degenerate by construction: with one candidate it IS the median, so both statistics
            # come out 0.00 dB however loud the channel is, and "clear" is guaranteed. Asking for
            # one frequency is the natural thing to do when checking a known channel, so this is
            # refused rather than answered with a reassuring zero.
            raise ValueError(
                "sense_spectrum needs at least two candidate frequencies: occupancy is measured "
                "against the median candidate, so a single channel is its own reference and always "
                "reads clear. Scan the channel of interest alongside references.")
        bw = float(bw_hz) if bw_hz else 400_000.0
        n = self.buffer_size
        powers, peaks = [], []
        for f in freqs_hz:
            rx = self._silent_capture(float(f), n)
            if rx.size < 16:
                powers.append(1e-12)
                peaks.append(0.0)
                continue
            psd = (np.abs(np.fft.fft(rx)) ** 2) / rx.size
            fr = np.fft.fftfreq(rx.size, 1.0 / self.sample_rate)
            inband = np.abs(fr) <= bw / 2.0
            band = psd[inband] if inband.any() else psd
            powers.append(float(band.mean()))
            peaks.append(float(band.max()))
        # BOTH statistics are normalised ACROSS channels, against the median channel. Normalising
        # the peak within its own band instead reads ~39 dB on every channel here, jammer or not:
        # the receiver's own local-oscillator spur is the loudest bin everywhere, so every channel
        # reports occupied and the agent can never find a clear one. Comparing like for like across
        # candidates cancels anything the receiver does identically at each.
        floor = float(np.median(powers)) or 1e-12
        peak_floor = float(np.median(peaks)) or 1e-12
        out = []
        for f, p, pk in zip(freqs_hz, powers, peaks):
            db = 10.0 * np.log10(p / floor + 1e-12)
            pk_db = 10.0 * np.log10(pk / peak_floor + 1e-12)
            out.append({"center_freq_hz": float(f), "power_db": round(float(db), 2),
                        "peak_db": round(float(pk_db), 2),
                        "occupied": bool(db > margin_db or pk_db > peak_margin_db),
                        "detect_margin_db": float(margin_db),
                        "peak_margin_db": float(peak_margin_db)})
        return out

    def close(self) -> None:
        """Stop the persistent flowgraph. gr-iio teardown can block; callers that just want
        the process to exit may prefer ``os._exit`` over a graceful close."""
        if self._tb is not None:
            try:
                self._tb.stop()
                self._tb.wait()
            except Exception:
                pass
            self._tb = None
            self._txsrc = self._txsink = self._rxsrc = self._rxsnk = None
            self._running_cfg = None

    # -- matched filter -> Gardner timing -> Costas carrier -> frame sync -> align --
    def _decode(self, rx, const, tx_bits, tx_syms, pre, sps, rolloff, costas_order):
        from gnuradio import blocks, digital, gr
        from gnuradio.filter import firdes
        from scipy.signal import correlate

        bps = const.bits_per_symbol
        n_syms = tx_syms.size
        n_pre = pre.size
        symrate = self.sample_rate / sps
        ntaps = self.ntaps_per_symbol * sps + 1
        rrc_full = np.asarray(
            firdes.root_raised_cosine(1.0, self.sample_rate, symrate, rolloff, ntaps)
        )
        dec = sps // 2  # matched-filter decimation -> ~2 samples/symbol into symbol_sync
        mf2 = np.convolve(rx, rrc_full)[::dec]
        mf2 -= mf2.mean()
        mf2 /= np.sqrt(np.mean(np.abs(mf2) ** 2)) + 1e-12

        tb = gr.top_block()
        src = blocks.vector_source_c(mf2.astype(np.complex64).tolist(), False)
        ss = digital.symbol_sync_cc(
            digital.TED_GARDNER, 2.0, self.timing_loop_bw, 1.0, 1.0, 1.5, 1,
            digital.constellation_qpsk().base(),
        )
        snk = blocks.vector_sink_c()
        if costas_order is not None:
            cl = digital.costas_loop_cc(self.carrier_loop_bw, costas_order, False)
            tb.connect(src, ss, cl, snk)
        else:
            tb.connect(src, ss, snk)  # 16-QAM: timing only here; carrier recovered below
        tb.run()
        sym = np.asarray(snk.data(), dtype=np.complex128)

        # Carrier recovery + frame sync differ by constellation family.
        if costas_order is not None:
            rx_syms, length, dbg = self._framesync_costas(sym, pre, n_syms)
        else:
            rx_syms, length, dbg = self._framesync_dd(sym, pre, tx_syms, const.points)

        # framework grading: decide bits/ideal points and align tx 1:1 to the overlap
        rx_bits, _ = const.hard_decision(rx_syms)      # decisions -> bits
        tx_syms_al = tx_syms[:length]
        tx_bits_al = tx_bits[: length * bps]
        rx_bits = rx_bits[: tx_bits_al.size]
        # data-aided EVM/SNR reference is the KNOWN tx (rx_syms is gain/phase-normalized), not decisions
        return rx_syms, tx_syms_al, tx_bits_al, tx_syms_al, rx_bits, dbg

    def _decode_zc(self, rx, const, tx_bits, tx_syms, pre, sps, rolloff, pilots=None):
        """Zadoff-Chu data-aided acquisition front-end (see link/sync.py).

        Matched-filter -> Schmidl-Cox detection + FFT-tone CFO + fine timing -> de-rotate ->
        residual carrier. Unlike the legacy path, no blind timing/carrier loop has to lock first, so
        acquisition survives well below the ~11 dB Gardner/Costas floor. Acquisition returns the exact
        payload boundary, so alignment for grading is direct. Modulation-independent.

        ``pilots=(n_payload, is_pilot)`` selects PSAM residual-carrier recovery: the payload carries a
        known pilot every N symbols and the carrier phase is INTERPOLATED from the pilots
        (rotation-invariant), instead of the decision-directed loop that can slip 90° on coded 16-QAM
        at marginal SNR. ``None`` keeps the blind DD loop.
        """
        from gnuradio.filter import firdes

        bps = const.bits_per_symbol
        n_syms = tx_syms.size                              # DATA symbols (grading target)
        n_payload = pilots[0] if pilots else n_syms        # transmitted payload (data + any pilots)
        carrier = "zc-psam" if pilots else "zc-dd"
        n_pre = pre.size
        symrate = self.sample_rate / sps
        ntaps = self.ntaps_per_symbol * sps + 1
        rrc_full = np.asarray(
            firdes.root_raised_cosine(1.0, self.sample_rate, symrate, rolloff, ntaps)
        )
        mf = np.convolve(rx, rrc_full)
        mf -= mf.mean()
        mf /= np.sqrt(np.mean(np.abs(mf) ** 2)) + 1e-12

        acq = sync.acquire(mf, sps, self.zc_half, self.zc_root, cfo_max=self.zc_cfo_max)
        if acq is None:
            # Honest no-lock: hand back unaligned zeros so the grader reports a failed link.
            length = n_syms
            rx_syms = np.zeros(n_syms, dtype=complex)
            dbg = {"carrier": carrier, "zc_locked": False, "frame_sync_metric": 0.0,
                   "frame_sync_tone": 0.0, "align_offset": -1}
        else:
            allsyms = sync.extract_symbols(mf, acq, n_pre + n_payload)
            # Anchor the ABSOLUTE constellation phase to the known ZC preamble before residual
            # carrier tracking. The decision-directed loop can otherwise settle on a 90°-rotated
            # alias of the rotationally-symmetric 16-QAM constellation -- benign-looking for an
            # uncoded link (it just measures as a bad link) but catastrophic for a coded one, where
            # a constant rotation permutes every symbol's bits and Viterbi decodes garbage. PSAM
            # (pilots) removes this entirely: the phase is measured from pilots, never decided.
            rx_pre = allsyms[:n_pre]
            if rx_pre.size:
                phi = float(np.angle(np.sum(rx_pre * np.conj(pre))))
                allsyms = allsyms * np.exp(-1j * phi)
            pay = allsyms[n_pre: n_pre + n_payload]
            pay = pay / (np.sqrt(np.mean(np.abs(pay) ** 2)) + 1e-12)
            # DD does the low-noise fine tracking (with the pilots as valid constellation points, so
            # it is not perturbed); the pilots then resolve the residual discrete 90° rotation (the
            # slip) that breaks coded 16-QAM -- robust where continuous pilot interpolation is not.
            pay = sync.dd_carrier(pay, const.points, self.dd_mu1, self.dd_mu2)
            if pilots:
                pay = sync.psam_slip_correct(pay, pilots[1], pilots[2])   # discrete 90° -> data only
            pay = pay / (np.sqrt(np.mean(np.abs(pay) ** 2)) + 1e-12)
            rx_syms = pay
            length = min(n_syms, rx_syms.size)
            dbg = {"carrier": carrier, "zc_locked": True,
                   "cfo_cyc_per_sym": float(acq["cfo"]),
                   "frame_sync_metric": float(acq["metric"]),
                   "frame_sync_tone": float(acq["tone"]),
                   "preamble_evm_pct": float(100.0 * np.sqrt(max(acq["preamble_evm"], 0.0))),
                   "align_offset": int(acq["offset"])}

        rx_bits, _ = const.hard_decision(rx_syms)      # decisions -> bits
        tx_syms_al = tx_syms[:length]
        tx_bits_al = tx_bits[: length * bps]
        rx_bits = rx_bits[: tx_bits_al.size]
        # data-aided EVM/SNR reference is the KNOWN tx (rx_syms is gain/phase-normalized), not decisions
        return rx_syms, tx_syms_al, tx_bits_al, tx_syms_al, rx_bits, dbg

    def _framesync_costas(self, sym, pre, n_syms):
        """Constant-modulus (BPSK/QPSK) path: Costas has already de-spun the stream, so the
        preamble correlates sharply. Plain argmax occasionally locks onto a spurious peak (low
        SNR / long payloads), so take the top-K correlation peaks and pick the one whose
        derotated preamble region best matches the KNOWN preamble (data-aided verification —
        the preamble is framework overhead)."""
        from scipy.signal import correlate

        n_pre = pre.size
        tail = sym[sym.size // 3:]
        tail = tail / (np.sqrt(np.mean(np.abs(tail) ** 2)) + 1e-12)
        blind_lock = float(np.abs(np.mean((tail / np.abs(tail)) ** 4)))
        sym_n = sym / (np.sqrt(np.mean(np.abs(sym) ** 2)) + 1e-12)
        corr = correlate(sym_n, pre, mode="valid", method="fft")
        limit = max(1, sym.size - (n_pre + n_syms))
        if corr[:limit].size == 0:
            raise RuntimeError("recovered stream too short for one frame")
        mean_mag = np.abs(corr).mean() + 1e-12
        mag = np.abs(corr[:limit]).copy()
        best = None  # (pre_evm, off, theta, sharp)
        for _ in range(6):
            off = int(np.argmax(mag))
            mag[max(0, off - 100):off + 100] = 0.0
            theta = float(np.angle(corr[off]))
            prec = sym[off:off + n_pre] * np.exp(-1j * theta)
            prec = prec / (np.sqrt(np.mean(np.abs(prec) ** 2)) + 1e-12)
            pre_evm = float(np.mean(np.abs(prec - pre) ** 2))  # data-aided vs known preamble
            sharp = float(np.abs(corr[off]) / mean_mag)
            if best is None or pre_evm < best[0]:
                best = (pre_evm, off, theta, sharp)
        pre_evm, off, theta, fs_sharp = best
        start = off + n_pre
        length = min(n_syms, sym.size - start)
        pay = sym[start:start + length] * np.exp(-1j * theta)
        pay = pay / (np.sqrt(np.mean(np.abs(pay) ** 2)) + 1e-12)
        dbg = {"carrier": "costas", "blind_lock": blind_lock,
               "frame_sync_sharpness": fs_sharp, "align_offset": off,
               "preamble_evm_pct": float(100.0 * np.sqrt(max(pre_evm, 0.0)))}
        return pay, length, dbg

    def _framesync_dd(self, sym, pre, tx_syms, points):
        """Non-constant-modulus (16-QAM) path. Costas fails, so the timing-only stream is
        carrier-rotated: the QPSK-preamble correlation is weak and picks spurious peaks. Robust
        recipe: take the top-K correlation peaks, keep only those with a real preamble (sharp
        data-aided FFT tone of seg*conj(preamble) — CFO-tolerant), de-spin each by that tone's
        CFO/phase, recover the payload with a decision-directed PLL, then let the (data-aided)
        grader fine-align to the known payload and select the frame with the lowest EVM. Frame
        selection + fine alignment against the known sequence is framework grading (cf. gr_sim)."""
        from scipy.signal import correlate

        n_pre = pre.size
        n_pay = tx_syms.size
        n_frame = n_pre + n_pay
        NF = 1 << 12
        freqs = np.fft.fftfreq(NF)
        if sym.size < n_frame + 1:
            raise RuntimeError("recovered stream too short for one frame")
        lim = sym.size - n_frame
        # Candidate offsets by PARTIAL-coherent correlation. A fully coherent correlation over
        # the whole preamble nulls out when the residual CFO reaches one cycle across it — and
        # this stream is deliberately not de-spun (Costas rings a 16-QAM constellation), so that
        # CFO is the raw two-radio offset. On the bench that is -0.0039 cyc/sym at 2.4 GHz
        # (-1020 Hz, -0.43 ppm) over a 256-symbol preamble = 1.00 cycles: exactly the null. The
        # true offset then falls out of the top-K and never reaches the CFO-tolerant tone gate
        # below, so every frame is rejected and the link reads as dead at a perfectly good SNR.
        # Correlating each sub-block coherently and summing MAGNITUDES is phase-blind between
        # blocks: tolerance widens to ~1 cycle per sub-block (nb x) for a small SNR cost.
        symn = sym / (np.abs(sym).mean() + 1e-9)
        nb = int(max(1, min(self.framesync_blocks, n_pre)))
        L = n_pre // nb
        if L < 1:
            nb, L = 1, n_pre
        c0 = np.zeros(lim)
        for b in range(nb):
            cb = np.abs(correlate(symn, pre[b * L:(b + 1) * L], mode="valid", method="fft"))
            c0 += cb[b * L:b * L + lim]
        mag = c0.copy()
        cands = []
        for _ in range(6):
            o = int(np.argmax(mag))
            cands.append(o)
            mag[max(0, o - 100):o + 100] = 0.0

        best = None  # (mse, rx_syms_aligned, off, tone)
        for o in cands:
            prod = sym[o:o + n_pre] * np.conj(pre)
            P = np.abs(np.fft.fft(prod, NF))
            k = int(np.argmax(P))
            tone = float(P[k] / (P.mean() + 1e-12))
            if tone <= 20.0:                       # not a real preamble at this offset
                continue
            seg = sym[o:o + n_frame] * np.exp(-1j * 2 * np.pi * freqs[k] * np.arange(n_frame))
            theta = np.angle(np.vdot(pre, seg[:n_pre]))
            pay = seg[n_pre:] * np.exp(-1j * theta)
            pay = pay / (np.sqrt(np.mean(np.abs(pay) ** 2)) + 1e-12)
            pay = self._dd_pll(pay, points)
            pay = pay / (np.sqrt(np.mean(np.abs(pay) ** 2)) + 1e-12)
            # data-aided fine alignment to the known payload (grader), small offset search
            w = min(512, n_pay // 2)
            cc = np.abs(correlate(pay, tx_syms[:w], mode="valid", method="fft"))
            d = int(np.argmax(cc[:64])) if cc.size > 64 else 0
            seg2 = pay[d:d + n_pay - 64]
            if seg2.size < n_pay // 2:
                continue
            ref = tx_syms[:seg2.size]
            seg2 = seg2 * np.exp(-1j * np.angle(np.vdot(ref, seg2)))
            seg2 = seg2 / (np.sqrt(np.mean(np.abs(seg2) ** 2)) + 1e-12)
            mse = float(np.mean(np.abs(seg2 - ref) ** 2))
            if best is None or mse < best[0]:
                best = (mse, seg2, o, tone)

        if best is None:  # no valid preamble anywhere -> honest failure via a raw pick
            off = int(np.argmax(c0[:lim]))
            length = min(n_pay, sym.size - (off + n_pre))
            pay = sym[off + n_pre:off + n_pre + length]
            pay = pay / (np.sqrt(np.mean(np.abs(pay) ** 2)) + 1e-12)
            return pay, length, {"carrier": "dd-pll", "frame_sync_sharpness": 0.0,
                                 "frame_sync_tone": 0.0, "align_offset": off}
        mse, seg2, off, tone = best
        dbg = {"carrier": "dd-pll", "frame_sync_sharpness": tone, "frame_sync_tone": tone,
               "align_offset": off, "grade_evm_pct": float(100.0 * np.sqrt(mse))}
        return seg2, seg2.size, dbg

    def _dd_pll(self, x: np.ndarray, points: np.ndarray) -> np.ndarray:
        """2nd-order decision-directed PLL. Tracks residual carrier phase and frequency using
        nearest-constellation-point decisions — the carrier recovery for non-constant-modulus
        constellations (16-QAM) where a Costas loop fails. Seeded by the preamble phase (``x``
        is already coarse-derotated), so the initial decisions are reliable."""
        mu1, mu2 = self.dd_mu1, self.dd_mu2
        phi = 0.0
        freq = 0.0
        out = np.empty_like(x)
        for i in range(x.size):
            y = x[i] * np.exp(-1j * phi)
            out[i] = y
            d = points[np.argmin(np.abs(y - points))]
            e = float(np.angle(y * np.conj(d)))
            freq += mu2 * e
            phi += mu1 * e + freq
        return out
