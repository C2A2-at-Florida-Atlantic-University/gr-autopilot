# Notation and abbreviations

Every abbreviation used anywhere in this documentation is defined here, along with the handful of
terms that carry a specific meaning in this system and could otherwise be read loosely. Entries
are grouped by subject rather than alphabetically, because related ideas are easier to hold
together than to look up one at a time.

## Measuring a link

Bit error ratio (BER)
: The proportion of received bits that differ from the bits transmitted. A ratio of 10⁻³ means one
  bit in a thousand arrived wrong. It is the primary measure of whether a link is working. Note
  "ratio", not "rate": it is dimensionless, not per unit time.

Error vector magnitude (EVM)
: How far received symbols land from where they should, expressed as a percentage of the signal
  amplitude. It describes the *quality* of what arrived, and unlike the bit error ratio it still
  distinguishes a good link from an excellent one after errors have stopped occurring altogether.

Signal-to-noise ratio (SNR)
: The ratio of wanted signal power to noise power, in decibels (dB). Higher is better; each 3 dB
  is a doubling.

Energy per symbol to noise spectral density ratio (Es/N₀)
: The form of signal-to-noise ratio used to specify a channel here, because it does not depend on
  how the signal is filtered or how many samples represent each symbol. When this documentation
  says "the channel is set to 12 dB", this is the quantity meant.

Decibel (dB)
: A logarithmic ratio: ten times the base-ten logarithm of a power ratio. Used because the
  quantities involved span many orders of magnitude.

## Describing a signal

Symbol
: One transmitted unit carrying a fixed number of bits. Sending symbols rather than bits directly
  is what allows a link to trade robustness against speed.

Constellation
: The set of points in the complex plane that symbols are drawn from, and by extension the
  scatter plot of received symbols. A clean link shows tight clusters at the ideal points; noise
  spreads them; an uncorrected frequency error smears them into a ring.

Modulation
: The scheme mapping bits to symbols. Three are used here:

    | Name | Bits per symbol | Points | Character |
    |---|---|---|---|
    | Binary phase-shift keying (BPSK) | 1 | 2 | Most robust, slowest |
    | Quadrature phase-shift keying (QPSK) | 2 | 4 | The usual compromise |
    | 16-point quadrature amplitude modulation (16-QAM) | 4 | 16 | Needs a good channel; the highest rung measured on the radios |
    | 8-phase-shift keying (8-PSK) | 3 | 8 | Constant modulus like QPSK, so a Costas loop tracks it; between QPSK and 16-QAM in rate and robustness |
    | 32-point quadrature amplitude modulation (32-QAM) | 5 | 32 | A cross-shaped grid; needs roughly 3 dB more than 16-QAM |
    | 64-point quadrature amplitude modulation (64-QAM) | 6 | 64 | Needs roughly 6 dB more than 16-QAM and an error vector near 10%; whether the radios reach that is measured, not assumed |
    | 256-point quadrature amplitude modulation (256-QAM) | 8 | 256 | Simulation only: free-running oscillators do not hold the ~5% error vector it needs |

Constant modulus
: A constellation whose points all have the same amplitude, such as BPSK and QPSK. Information is
  carried entirely in phase. 16-QAM is **not** constant modulus — it uses amplitude as well —
  which is why some receiver techniques work for the first two and not the third.

Samples per symbol (sps)
: How many digital samples represent each symbol. More samples give the receiver more to work with
  when deciding exactly when a symbol occurs, at the cost of bandwidth and computation.

Root-raised-cosine (RRC) filter
: The pulse shape used at both transmitter and receiver. Splitting the shaping between the two
  ends means the combination has no inter-symbol interference at the sampling instants, while
  each end individually limits the occupied bandwidth.

Excess bandwidth (roll-off)
: A number between 0 and 1 setting how much wider than the theoretical minimum the filter is. A
  smaller value occupies less spectrum but makes the receiver's timing more critical.

Spectral efficiency
: Bits carried per symbol. Higher-order modulation buys efficiency and spends channel quality.

## Receiving

Symbol timing recovery
: Working out the instant within each symbol at which to sample. Without it, a receiver has no
  symbol grid and nothing downstream can work. The detector used here is the **Gardner** timing
  error detector, which compares a sample at the symbol centre with one midway between symbols,
  and therefore needs two samples per symbol.

Matched filter
: A receive filter shaped like the transmitted pulse. It maximises the signal-to-noise ratio at
  the sampling instant; removing it measurably degrades a link.

Carrier frequency offset (CFO)
: The difference between the transmitter's and the receiver's oscillator frequencies. Two
  independent radios never agree exactly. An uncorrected offset rotates the constellation
  continuously.

Costas loop
: A carrier recovery technique that estimates and removes the frequency and phase offset. It
  requires a constant-modulus constellation, so it works for BPSK and QPSK but rings a 16-QAM
  constellation rather than locking it.

Automatic gain control (AGC)
: Normalises the amplitude of a received signal. It matters most for constellations that carry
  information in amplitude, such as 16-QAM.

Zadoff–Chu (ZC) sequence
: A sequence with a sharp autocorrelation peak, used as a preamble so the receiver can find the
  start of a frame and estimate the frequency offset even before anything has locked.

Forward error correction (FEC)
: Adding structured redundancy so some errors can be corrected rather than merely detected. The
  convolutional code used here is decoded with the **Viterbi** algorithm.

## Adapting

Adaptive modulation and coding (AMC)
: Choosing the modulation, and the amount of error correction, to suit the channel — the highest
  spectral efficiency that still meets the error target.

Frequency avoidance
: Moving the link to a different frequency after finding interference on the current one.

Frequency hopping
: Moving the link between several frequencies repeatedly, so an interferer that follows can only
  catch it part of the time.

Reactive interferer
: One that transmits only when the link does. It reads as clear to a listener, because it is
  silent while being listened for, which is why an agent can sense an empty channel and still
  fail on it.

## This system's own terms

Flowgraph
: A connected set of signal-processing blocks. In GNU Radio this is the unit of execution; here it
  is also the unit of design, the thing the agent composes and the thing you can take away.

Skill
: One named stage in a chain, such as `rrc_matched_filter`, which the system realises as a real
  GNU Radio block.

Chain
: An ordered list of skills. `tx_chain` turns bits into a signal; `rx_chain` turns a received
  signal back into symbols.

Experiment
: One investigation, with a goal and a start and end time. It contains iterations and may produce
  artifacts.

Iteration
: One recorded step within an experiment: what changed, and what was measured. An iteration where
  a radio disappeared is recorded with **no** measurement rather than a fabricated one.

Artifact
: A file an experiment produced — currently an exported flowgraph.

The framework
: The parts of the system the agent cannot see or influence: the known payload, the error
  counting, the channel condition, and whether an interferer is present.

Hidden condition
: The channel quality and interference state the operator sets and the agent must discover by
  measurement. Never disclosed through any tool, prompt, or exported file.

## Software and protocols

GNU Radio
: The open-source signal-processing toolkit whose blocks this system composes.

GNU Radio Companion (GRC)
: The graphical editor shipped with GNU Radio. Its files use the extension `.grc` and are what
  this system exports and re-imports.

Model Context Protocol (MCP)
: The protocol by which a language model is offered tools and prompts. This system speaks it in
  two ways: over standard input and output, and over HTTP, described under
  [system architecture](/wiki/architecture).

Streamable HTTP
: The MCP transport in which the server listens on a URL and clients send requests to it, as
  opposed to the client launching the server as a child process.

JavaScript Object Notation (JSON)
: The text format used for tool arguments and results.

SQLite
: The embedded database, part of the Python standard library, in which experiments and iterations
  are stored.

Industrial input/output (IIO)
: The Linux subsystem through which the PlutoSDR radios are reached. The `libiio` library and its
  `iio_attr` command-line tool appear in device-health checks.

Software-defined radio (SDR)
: A radio whose signal processing is done in software. The devices used here are **ADALM-Pluto**
  units, referred to as PlutoSDR.
