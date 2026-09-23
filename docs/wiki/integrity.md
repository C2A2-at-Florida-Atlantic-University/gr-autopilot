# Measurement integrity

The claim this system makes is that the agent discovers the state of the channel by measuring it.
That claim is only worth as much as the care taken to keep the answer away from it. This page
states what is hidden, how it is kept hidden, and how that is checked.

## What is hidden, and why

The **hidden condition** is the channel quality the operator sets and whether an interferer is
present. The agent is never told either.

The reason is that being told would make the exercise meaningless. An agent informed that the
channel is at a particular signal-to-noise ratio can select a modulation from a textbook table
without measuring anything, and the resulting demonstration would show only that it can read a
table. The interesting question is whether it can find the answer from evidence, and that
requires the evidence to be all it has.

The same applies to interference. The countermeasure exercises present a **symptom** — the link
fails, and lowering the modulation order does not help — and leave the diagnosis to the agent.
Being told that a jammer exists would remove the diagnosis, which is the part being tested.

## The division of labour

| The framework owns | The agent owns |
|---|---|
| The transmitted payload | The signal-processing chain |
| Counting errors against that payload | The modulation and coding |
| The channel condition | The centre frequency and any hop plan |
| Whether an interferer exists | The transmit power, within a budget |
| Which radio is the grader | Its own reasoning, recorded as it goes |

Error counting is framework-owned because the framework holds the payload it transmitted. This is
what makes the resulting error ratio a measurement rather than a claim: it is counted against
known truth, not against the receiver's own guesses.

## Where a leak could occur, and what guards each

There are five surfaces through which the hidden condition could escape. Each is guarded, and the
guards are tests rather than intentions.

**Tool responses.** Every tool in the published surface is driven against a service seeded with
distinctive sentinel values, and no response may contain them. The test enumerates the tool list
itself, so **a newly added tool without a leak check fails the suite** rather than silently
skipping the scan. That guard has fired twice during development, both times correctly.

**The recorded transcript.** The same scan is applied to the protocol transcript written to disk,
because a leak could hide in a log rather than in a reply.

**Exported flowgraph files.** An exported file is an artifact the agent can request and read. Its
channel noise amplitude is computed from the hidden channel quality, so writing the true value
would disclose by arithmetic exactly what every tool response withholds. A placeholder is written
instead, the return value says so, and a test asserts the true value never reaches the file.

**Prompt text.** The [experiment library](/wiki/library) is free text handed straight to the
agent — the easiest place in the system to give the answer away, and the only surface no
response-based guard would ever inspect. A single sentence such as *"the channel is at 12 dB, so
start with QPSK"* would end the claim. Prompts describe a goal and a method, never an answer, and
their bodies are read by tests: no channel condition in words or as a numeric decibel value, no
assertion that an interferer exists, no naming of which modulation will win. They are rendered
with every argument substituted as well as with defaults, because an argument lands inside the
text.

**Database columns.** Records hold what the agent may see. The hidden channel setpoint is stripped
at the single point where a snapshot is serialised.

## What the agent is legitimately told

It would be equally dishonest to withhold what a real engineer could measure. The agent is told:

- Every measurement made against the known payload: error ratio, error vector magnitude,
  estimated signal-to-noise ratio.
- The recovered constellation, and a diagnosis of its shape.
- Received power at candidate frequencies while its own transmitter is quiet — energy detection,
  which is a real technique and genuinely reveals a *non-reactive* interferer. A reactive one
  reads as clear, which is why an agent can sense an empty channel and still fail on it, and why
  that failure is informative rather than a trick.
- The fixed properties of the radio-frequency path: cabling and attenuation.
- Its own history.

The estimated signal-to-noise ratio deserves a note. It is computed against the **known**
transmitted symbols rather than against the receiver's decisions. Referencing the decisions would
make it a decision-directed quantity, optimistically biased at low signal levels — flattering
exactly when accuracy matters most.

## Honest failure

Integrity is not only about withholding. Three cases could each produce a number that looks like
a measurement but is not one:

- **A lost radio.** A stalled receiver still returns buffers grading near one half. Recorded as
  aborted with no measurement. See [radios and device health](/wiki/devices).
- **An unsynchronised chain.** A receive chain that never reaches one sample per symbol has no
  symbol grid. Reported as such, rather than rescued by the framework searching sampling phase on
  the agent's behalf.
- **An interferer that failed to start.** A jammer that did not launch would make the agent look
  as though it had evaded something. Its process is checked after starting rather than assumed.

The common principle: **the absence of a measurement must never be recorded as a measurement.**
