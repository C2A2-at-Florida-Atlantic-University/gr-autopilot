"""The experiment library: the three experiments reported in the paper, expressed as prompts.

Each is a prompt and nothing else -- no saved flowgraph, no script, no fixture. The agent has to
do the work through the same tools anyone else would use, which is the point: if a prompt in
this library cannot be carried out with the published tools, then the tools are incomplete, and
that shows up here rather than being hidden by a prepared asset.

Prompts are parameterised, so the same experiment can be run at a different error-ratio target
or over a different number of channels without rewriting it.

INTEGRITY. A prompt is free text handed straight to the agent, which makes it the easiest place
in the whole system to give away the answer -- a sentence such as "the channel is at 12 dB, so
start with QPSK" would quietly destroy the central claim that the agent discovers conditions by
measurement. Nothing here states, implies, or hints at the hidden channel condition, the presence
or absence of an interferer, or which modulation will win. Prompts describe a GOAL and a METHOD.
The tests enforce this (see ``tests/test_prompts.py``), and the exhaustive leak scan drives every
prompt body as well as every tool response.
"""
from __future__ import annotations

from dataclasses import dataclass, replace, field


@dataclass(frozen=True)
class PromptArg:
    name: str
    description: str
    required: bool = False
    default: str = ""

    def to_mcp(self) -> dict:
        return {"name": self.name, "description": self.description, "required": self.required}


@dataclass(frozen=True)
class Prompt:
    name: str
    title: str
    description: str
    body: str                                   # formatted with the arguments
    arguments: tuple[PromptArg, ...] = ()
    topic: str = "general"
    #: Pins the exercise to one venue when it only makes sense there. Such a prompt takes no
    #: ``where`` argument -- the choice is not the caller's -- and always renders that venue's
    #: note. The paper experiments are pinned to ``hardware``: each one's result is a claim about
    #: what radios do, and a simulated run of one would read identically while measuring a model.
    venue: str = ""
    #: What the OPERATOR should know before running it on this bench: what to arm, which number
    #: to trust, which caveat applies today. Rendered in the wiki and the portal, never in the
    #: prompt -- a note may say that an interferer will be keyed, which the prompt must not.
    notes: str = ""

    def to_mcp(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description,
                "arguments": [a.to_mcp() for a in self.arguments]}

    def render(self, arguments: dict | None = None) -> str:
        values = {a.name: a.default for a in self.arguments}
        for key, value in (arguments or {}).items():
            if key in values and value not in (None, ""):
                values[key] = str(value)
        text = self.body.format(**values)
        where = values.get("where", "") or self.venue
        if where:
            text += "\n\n" + _WHERE_NOTE[where if where in _WHERE_NOTE else "simulation"]
        return text.strip()


_WHERE_NOTE = {
    "simulation": (
        "Run this on the simulated channel. Call `list_devices` first and say which backend you "
        "are on, so the reader knows whether these numbers came from a model or from radios."),
    "hardware": (
        "Run this on the radios. Call `list_devices` first and confirm both are present and "
        "answering; if one is not, stop and say so rather than reporting a measurement. If a "
        "radio disappears mid-experiment the tools will tell you it is unavailable -- that is not "
        "a bad measurement, it is the absence of one, and it should be reported as such."),
}

_TARGET = PromptArg("target_ber", "the bit error ratio to stay at or below", default="0.01")
_TRIALS = PromptArg("trials", "how many trials per rung", default="10")
_CHANNELS = PromptArg("channels", "how many channels the hop plan should span", default="four")
_BASELINE_TRIALS = PromptArg("baseline_trials",
                             "full-length trials to establish the camping baseline", default="5")


PROMPTS: tuple[Prompt, ...] = (
    Prompt(
        name="find-the-ceiling", title="Experiment 1 — the modulation staircase",
        topic="paper", venue="hardware",
        description=("Climb every rung the framework offers, several trials each, and say where "
                     "the link stops and what stopped it. The paper's first experiment."),
        arguments=(_TARGET, _TRIALS),
        body=(
            "Find the highest-order modulation this link will carry at a bit error ratio of "
            "{target_ber} or better, trying every rung the framework offers.\n\n"
            "`list_skills` names the modulators; climb in order of bits per symbol. Take "
            "{trials} trials per rung, and read every result beside the number of bits it "
            "graded -- a trial that graded fewer bits than you asked for is not the same "
            "measurement as one that graded them all, so keep the two apart when you bound the "
            "error ratio, and say how many trials of each kind each rung got. At each rung that "
            "fails, `diagnose_signal` before moving on, and capture the constellation: a rung can "
            "fail from noise, from an unlocked carrier, or from a link that dropped out mid-run, "
            "and those mean different things about the ceiling. The operating point is fixed in "
            "hardware by me and is not yours to change -- do not try to move transmit power or "
            "gain to make a rung pass; measure the link as you find it.\n\n"
            "Report the ladder with, per rung, the error ratio from full-length trials, the error "
            "vector magnitude and its spread, the graded-bit count, and the diagnosis. Say which "
            "rung you settled on and what stopped the next one -- the constellation's decision "
            "distance, the carrier recovery, or the link's stability -- and how confident you are "
            "that it is the hardware's ceiling rather than this operating point's."),
    ),
    Prompt(
        name="find-the-interference", title="Experiment 2 — the fixed jammer",
        topic="paper", venue="hardware",
        description=("Hold the best rung while the environment changes underneath you: diagnose "
                     "the failure, sense the band with the link silent, retune and re-climb. "
                     "The paper's second experiment."),
        arguments=(_TARGET,),
        body=(
            "Hold this link at a bit error ratio of {target_ber} or better, at the highest-order "
            "modulation that meets it, and keep it there while I change the environment around "
            "you without telling you what I changed.\n\n"
            "At some point the link will degrade. Do not assume why. `diagnose_signal` and "
            "capture the constellation before you react, and say what the pair of numbers "
            "implies: a poor error ratio with a tight constellation is not the same fault as a "
            "poor one with a smeared constellation, and only one of them is fixable by moving. If "
            "you suspect the channel is occupied rather than merely weak, stop transmitting and "
            "sense the band: sweep candidate channels with `sense_spectrum` while your own link "
            "is silent, and report the power per channel, so the occupied one can be seen "
            "standing above its neighbours rather than asserted. Stay inside the band the RF path "
            "config permits. Then `set_center_freq` to the channel you judge clearest, "
            "re-establish, and climb again -- and confirm by measurement, not by assumption, that "
            "the rung you had before is back.\n\n"
            "Report the whole episode as a timeline: baseline rung and error ratio, the failure "
            "and its graded numbers, the diagnosis and what ruled out the alternatives, the "
            "sensed spectrum with the margin between the occupied channel and the clear ones, the "
            "channel you chose and why, and the recovered rung with its error ratio and "
            "constellation. If any state from the degraded link had to be cleared before the new "
            "channel measured cleanly, say so explicitly."),
    ),
    Prompt(
        name="out-hop-the-jammer", title="Experiment 3 — the following jammer",
        topic="paper", venue="hardware",
        description=("Measure camping against hopping under an interferer that chases, sweep the "
                     "hop rate, and state the boundary as a measurement rather than a claim. "
                     "The paper's third experiment."),
        arguments=(_TARGET, _CHANNELS, _BASELINE_TRIALS),
        body=(
            "The interferer in this session does not stay put -- it chases. Keep the link alive "
            "at a bit error ratio of {target_ber} or better under a jammer that follows you, and "
            "measure how well the defence actually works.\n\n"
            "First establish and measure the camping case: one fixed channel, your best rung, at "
            "least {baseline_trials} full-length trials, with the error ratio, the graded-bit "
            "count and a constellation. Then `set_hop_plan` over {channels} channels inside the "
            "permitted band at a dwell you choose, and measure the same link again under the same "
            "interferer. Report the dwell and the hop rate you used, and why that dwell.\n\n"
            "Report the aggregate error ratio as total bit errors divided by total graded bits "
            "over the hop cycle -- not as the mean of the per-trial ratios, which is a different "
            "quantity -- and give both, plus the per-dwell spread, plus the fraction of dwells "
            "that were hit. Then sweep the hop rate over at least three settings and report the "
            "same three numbers at each, and say whether the trend is what the channel count "
            "predicts or something else.\n\n"
            "Close with the boundary, stated as measurement rather than claim: how long the "
            "interferer actually needs to reach a new channel, how that compares with your "
            "dwell, and therefore which of these two you demonstrated -- a follower slower than "
            "the dwell, which hopping out-runs, or one fast enough to arrive inside it, which it "
            "does not. If you could not measure the interferer's chase time from your own "
            "instruments, say that, and say what the result does and does not establish."),
    ),
)

# Operator-facing notes, by name. Kept apart from the bodies so that what the agent is told and
# what the operator is told cannot be confused; a test checks they never cross.
_NOTES: dict[str, str] = {
    "find-the-ceiling": (
        "Paper Experiment 1. The operating point is the operator's: set tx_atten_db from the "
        "Operator page BEFORE the run and leave it alone, because the prompt tells the agent not "
        "to touch power or gain and the result is only a ceiling if that held. Measured "
        "2026-09-08 at tx_atten 18 dB: 8-PSK 0 errors in 240k full-length bits, 32-QAM 2.6e-4, "
        "64-QAM 0 errors in 160k bits across three full-length trials -- but the link at that "
        "budget was intermittently unstable (SNR swung 28 -> -3 dB across consecutive trials), so "
        "a SUSTAINED 64-QAM link is not yet shown and 256-QAM has not had a clean test. Lower "
        "rx_gain below 45 dB when raising the transmit budget; the receive front end is the "
        "likely culprit. Expect roughly a third of trials to grade short -- that is why the "
        "prompt insists the two populations are counted separately."),
    "find-the-interference": (
        "Paper Experiment 2. Arm the interferer from the Operator page AFTER the agent has a "
        "baseline rung, and do not announce it -- the whole experiment is whether the diagnosis "
        "is reached from the instruments. The prompt describes only the symptom and must never "
        "say an interferer exists; a test reads every prompt for that. Note that the quietest "
        "channel a sweep finds is not always the best one to move to: energy detection cannot "
        "tell an empty channel from one the antenna does not couple to, and a retune needs "
        "build_flowgraph called again before the new channel measures anything but placeholders."),
    "out-hop-the-jammer": (
        "Paper Experiment 3. Arm with jammer_follow: true. The flowgraph follower retunes in "
        "0.37 ms and hopping loses to it; the CLI follower of the older results took 1.77 s and "
        "hopping beat it. Which of those is on the bench decides which result the run can honestly "
        "claim, which is why the prompt closes on the chase time rather than on the hop rate -- "
        "the honest limit is the retune time of whatever follows you."),
}

PROMPTS = tuple(replace(p, notes=_NOTES.get(p.name, "")) for p in PROMPTS)
assert not set(_NOTES) - {p.name for p in PROMPTS}, "a note names a prompt that does not exist"

_BY_NAME = {p.name: p for p in PROMPTS}


def list_prompts() -> list[dict]:
    return [p.to_mcp() for p in PROMPTS]


def get_prompt(name: str, arguments: dict | None = None) -> dict:
    """The MCP ``prompts/get`` payload: a description plus the message to send."""
    prompt = _BY_NAME.get(name)
    if prompt is None:
        raise KeyError(name)
    return {"description": prompt.description,
            "messages": [{"role": "user",
                          "content": {"type": "text", "text": prompt.render(arguments)}}]}


def catalog() -> list[dict]:
    """Everything about every prompt, for the portal's library page."""
    return [{"name": p.name, "title": p.title, "topic": p.topic, "description": p.description,
             "arguments": [{"name": a.name, "description": a.description, "default": a.default,
                            "required": a.required} for a in p.arguments],
             "body": p.render(), "notes": p.notes}
            for p in PROMPTS]
