"""The experiment library.

Two things are being protected. The first is that these remain PROMPTS: text, parameterised, with
no prepared flowgraph or fixture behind them. If an exercise here cannot be carried out with the
published tools, that is a gap in the tools, and it should be visible rather than papered over by
a saved asset.

The second matters more. A prompt is free text handed straight to the agent, so it is the easiest
place in the entire system to give away the answer. One sentence -- "the channel is at 12 dB, so
start with QPSK" -- would end the claim that the agent discovers conditions by measurement, and no
existing test would have noticed, because every other guard inspects tool responses. These tests
read the prompt bodies themselves.
"""
import json
import re
import urllib.request

import pytest

from gr_autopilot.telemetry.server import serve
from gr_autopilot.tools import prompts
from gr_autopilot.tools.mcp_stdio import StdioMCPServer


def _bodies() -> list[tuple[str, str]]:
    """Every prompt rendered with defaults AND with each argument varied, since an argument is
    substituted into the text and could carry a disclosure that the default does not."""
    out = []
    for p in prompts.PROMPTS:
        out.append((p.name, p.render()))
        for arg in p.arguments:
            out.append((f"{p.name}[{arg.name}]", p.render({arg.name: "16qam"})))
    return out


# -- integrity ---------------------------------------------------------------

@pytest.mark.parametrize("name,body", _bodies())
def test_no_prompt_states_the_hidden_channel_condition(name, body):
    """The agent must not be told the signal-to-noise ratio, in words or numbers."""
    lowered = body.lower()
    for phrase in ("es/n0", "es_n0", "snr is", "signal-to-noise ratio is", "the channel is at",
                   "db snr", "noise level is"):
        assert phrase not in lowered, f"{name} discloses the channel: {phrase!r}"
    # A decibel figure attached to the channel would be a disclosure. Decibels appear legitimately
    # (coding gain, "express the gain in decibels"), so the guard is on a NUMBER next to one.
    numeric_db = re.findall(r"-?\d+(?:\.\d+)?\s*d[bB]\b", body)
    assert not numeric_db, f"{name} contains a numeric decibel value: {numeric_db}"


@pytest.mark.parametrize("name,body", _bodies())
def test_no_prompt_reveals_whether_an_interferer_is_present(name, body):
    """Stage-two and Stage-three exercises describe a SYMPTOM the agent must diagnose. Asserting
    that a jammer exists would hand over the diagnosis."""
    lowered = body.lower()
    for phrase in ("a jammer is", "there is a jammer", "the jammer is on", "an interferer is on",
                   "jammer at", "interferer at"):
        assert phrase not in lowered, f"{name} gives away the interferer: {phrase!r}"


@pytest.mark.parametrize("name,body", _bodies())
def test_no_prompt_names_the_answer(name, body):
    """Telling the agent which modulation will win removes the experiment."""
    lowered = body.lower()
    for phrase in ("use qpsk because", "16qam will work", "qpsk is the answer",
                   "the correct modulation", "you should end up with"):
        assert phrase not in lowered, f"{name} names the outcome: {phrase!r}"


# -- shape -------------------------------------------------------------------

def test_the_library_is_not_empty_and_names_are_unique():
    names = [p.name for p in prompts.PROMPTS]
    assert len(names) == 3                        # the paper's three experiments
    assert len(set(names)) == len(names)


@pytest.mark.parametrize("p", prompts.PROMPTS, ids=lambda p: p.name)
def test_every_prompt_renders_without_a_missing_argument(p):
    text = p.render()
    assert len(text) > 120                        # a real exercise, not a stub
    assert "{" not in text and "}" not in text    # every placeholder was substituted


@pytest.mark.parametrize("p", prompts.PROMPTS, ids=lambda p: p.name)
def test_every_prompt_asks_for_something_to_be_reported(p):
    """An exercise that produces no stated finding teaches nothing."""
    assert re.search(r"\b(report|tell me|say|present|explain|compare|describe)\b",
                     p.render(), re.I)


@pytest.mark.parametrize("p", prompts.PROMPTS, ids=lambda p: p.name)
def test_every_tool_a_prompt_names_actually_exists(p):
    """A prompt that instructs the agent to call a tool which is not published would send it
    looking for something that is not there -- and is a sign the library and the tool surface
    have drifted apart."""
    published = {t.name for t in StdioMCPServer().tools}
    referenced = set(re.findall(r"`([a-z_]+)`", p.render()))
    # Backticks are also used for skill names; only check the ones that look like tool calls.
    unknown = {r for r in referenced
               if r not in published and r in {"set_center_freq", "set_hop_plan", "set_tx_power",
                                               "sense_spectrum", "export_flowgraph",
                                               "import_flowgraph", "capture_spectrum",
                                               "capture_constellation", "annotate_ledger",
                                               "list_devices", "list_skills",
                                               "describe_experiment", "diagnose_signal"}}
    assert not unknown, f"{p.name} refers to unpublished tools: {unknown}"


def test_arguments_are_substituted_into_the_text():
    p = next(x for x in prompts.PROMPTS if x.name == "out-hop-the-jammer")
    text = p.render({"target_ber": "0.001", "channels": "six", "baseline_trials": "8"})
    assert "0.001" in text and "six channels" in text and "8 full-length trials" in text


def test_an_unknown_argument_is_ignored_rather_than_crashing():
    p = prompts.PROMPTS[0]
    assert p.render({"nonsense": "x"})            # does not raise


def test_hardware_and_simulation_get_different_guidance():
    """A prompt that leaves the venue to the caller renders that venue's note."""
    p = prompts.Prompt(name="t", title="t", description="t", body="Build a link and report it.",
                       arguments=(prompts.PromptArg("where", "simulation or hardware",
                                                    default="simulation"),))
    sim, hw = p.render({"where": "simulation"}), p.render({"where": "hardware"})
    assert sim != hw
    assert "radios" in hw and "absence of one" in hw     # device loss is explained on hardware
    assert "simulated channel" in sim


def test_the_paper_experiments_are_pinned_to_the_radios():
    """Experiments 1-3 are claims about what radios do. Run against a model they would render
    identically and measure something else, so the venue is not the caller's to choose: they
    carry no ``where`` argument, and passing one anyway does not move them off the bench."""
    paper = [p for p in prompts.PROMPTS if p.topic == "paper"]
    assert {p.name for p in paper} == {"find-the-ceiling", "find-the-interference",
                                       "out-hop-the-jammer"}
    for p in paper:
        assert "where" not in {a.name for a in p.arguments}, p.name
        assert p.venue == "hardware", p.name
        text = p.render()
        assert "Run this on the radios" in text, p.name
        assert "simulated channel" not in text, p.name
        assert p.render({"where": "simulation"}) == text, p.name


# -- the protocol ------------------------------------------------------------

def test_prompts_are_advertised_and_listed_over_the_protocol():
    server = StdioMCPServer()
    caps = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {}})["result"]["capabilities"]
    assert "prompts" in caps                       # a client only asks if the server says so
    listed = server.handle({"jsonrpc": "2.0", "id": 2,
                            "method": "prompts/list"})["result"]["prompts"]
    assert len(listed) == len(prompts.PROMPTS)
    assert {"name", "title", "description", "arguments"} <= set(listed[0])


def test_getting_a_prompt_returns_a_user_message():
    server = StdioMCPServer()
    got = server.handle({"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
                         "params": {"name": "find-the-ceiling",
                                    "arguments": {"target_ber": "0.001"}}})["result"]
    message = got["messages"][0]
    assert message["role"] == "user"
    assert "0.001" in message["content"]["text"]


def test_an_unknown_prompt_is_an_invalid_parameter():
    server = StdioMCPServer()
    reply = server.handle({"jsonrpc": "2.0", "id": 4, "method": "prompts/get",
                           "params": {"name": "no-such-prompt"}})
    assert reply["error"]["code"] == -32602


# -- the portal --------------------------------------------------------------

def test_the_portal_serves_the_library():
    httpd = serve(None, None, port=8152, background=True)
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8152/prompts",
                                                 timeout=5).read())
        assert len(data["prompts"]) == len(prompts.PROMPTS)
        first = data["prompts"][0]
        assert {"name", "title", "topic", "description", "arguments", "body"} <= set(first)
    finally:
        httpd.shutdown()


# -- operator notes stay on the operator's side ------------------------------

@pytest.mark.parametrize("p", [p for p in prompts.PROMPTS if p.notes], ids=lambda p: p.name)
def test_bench_notes_never_reach_the_agent(p):
    """A note may say that an interferer will be keyed or which rung wins; the prompt must not.
    So the note is not in the rendered prompt, not in the MCP listing, and not in prompts/get."""
    assert p.notes not in p.render()
    assert "notes" not in p.to_mcp()
    text = prompts.get_prompt(p.name)["messages"][0]["content"]["text"]
    assert p.notes not in text
