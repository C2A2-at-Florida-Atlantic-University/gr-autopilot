"""The integrity split, exhaustively: NO manifest tool may leak the hidden channel.

The paper's central claim is that the agent tool surface never reveals ground truth (true SNR,
interferer strength/reaction). Per-tool leak checks were previously scattered over a hand-picked
subset with inconsistent forbidden-key lists — a refactored or new tool that echoed the hidden
condition could pass the whole suite. This drives EVERY tool in the manifest against a service seeded
with distinctive sentinel hidden state and asserts none of it appears in any serialized response.
"""
from __future__ import annotations

import json
from pathlib import Path

from gr_autopilot.flowgraph import FlowgraphSpec
from gr_autopilot.link.interference import Interferer
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer
from gr_autopilot.tools.mcp_stdio import MCPToolError

# Distinctive, full-precision hidden values chosen NOT to be reproduced by any legal measurement:
#   * CLEAN is a very high Es/N0; with the reactive jammer on-channel the measured (data-aided) SNR is
#     dragged far below it, so the estimate never prints "41.6187".
#   * RS (reaction latency) is never measured at all.
CLEAN_ES_N0, JAM_INR, JAM_REACTION = 41.6187, 33.9042, 0.0041773

# Forbidden = the hidden KEY NAMES (strongest guard: catches a leaked meta dict regardless of value)
# plus the two truly-unmeasurable hidden VALUES. INR's value is deliberately NOT forbidden — energy
# detection legitimately measures received power ~INR, so that is a designed measurement, not a leak.
FORBIDDEN = ["clean_es_n0_db", "effective_es_n0_db", "inr_db", "reaction_s", "interferer_present",
             str(CLEAN_ES_N0), str(JAM_REACTION)]


def _service(tmp_path):
    jam = Interferer(center_freq_hz=2.401e9, inr_db=JAM_INR, reactive=True, reaction_s=JAM_REACTION)
    be = InterferenceSimBackend(clean_es_n0_db=CLEAN_ES_N0, interferer=jam, center_freq_hz=2.4e9)
    return AutopilotService(backend=be, ledger_path=tmp_path / "leak.jsonl")


def test_no_manifest_tool_leaks_hidden_channel_state(tmp_path):
    svc = _service(tmp_path)
    cli = InProcessClient(StdioMCPServer(svc))
    names = [t["name"] for t in cli.list_tools()]

    a_block = cli.call("list_blocks")[0]["id"]
    a_skill = cli.call("list_skills")[0]["name"]
    spec = FlowgraphSpec.link("qpsk").to_dict()

    # Best-effort VALID arguments for every tool. Driven in manifest order, so prerequisites
    # (build_flowgraph before run_flowgraph before get_metrics/diagnose; author before promote) hold.
    args = {
        # experiment lifecycle first, as the manifest orders it: everything below records into it
        "current_experiment": {}, "list_saved_experiments": {},
        "start_experiment": {"name": "leak probe", "goal": "scan every tool"},
        "switch_experiment": {"name": "leak probe"},
        "rename_experiment": {"name": "leak probe renamed"},
        "list_blocks": {}, "describe_block": {"name": a_block},
        "list_skills": {}, "describe_skill": {"name": a_skill},
        "list_devices": {}, "probe_device": {"device_id": svc.tx_device},
        "get_rf_path_config": {}, "list_experiments": {},
        "describe_experiment": {"name": "link_adaptive"},
        "validate": {"spec": spec}, "build_flowgraph": {"spec": spec},
        "claim_device": {"device_id": svc.tx_device, "role": "transmitter"},
        "run_flowgraph": {"n_bits": 8000}, "get_status": {},
        "get_metrics": {}, "capture_constellation": {"max_points": 64},
        "capture_spectrum": {}, "diagnose_signal": {},
        "sense_spectrum": {"freqs_hz": [2.400e9, 2.401e9, 2.405e9]},
        "set_center_freq": {"center_freq_hz": 2.405e9},
        "set_hop_plan": {"channels_hz": [2.400e9, 2.401e9, 2.402e9], "hop_rate_hz": 500.0},
        "clear_hop_plan": {}, "set_tx_power": {"power_db": 3.0},
        "author_skill": {"name": "leak_probe_rung", "ladder": ["qpsk", "16qam", "16qam:conv_k3_r34"]},
        "promote_skill": {},
        "start_bo_run": {"params": ["phase_correction_rad"], "bounds": [[-0.5, 0.5]],
                         "budget": 3, "trial_bits": 4000},
        "get_bo_status": {}, "stop_bo_run": {},
        "read_edit_ledger": {"compact": True},
        "annotate_ledger": {"iteration": 1, "note": "leak probe"},
        "export_flowgraph": {"path": str(tmp_path / "leak_probe.grc")},
        # Driven against the file the export above just wrote, so the import path is exercised
        # with a real graph rather than failing early on a missing file.
        "import_flowgraph": {"path": str(tmp_path / "leak_probe.grc")},
        # Concluding the run returns the experiment's OUTCOME -- counts, the best graded trial,
        # every decision taken -- so it is exactly the sort of summary a leak could ride out on.
        "finish_experiment": {"summary": "leak probe concluded"},
    }
    # Coverage guard: a NEW tool added to the manifest without an entry here fails loudly, so the leak
    # scan can never silently skip a tool.
    missing = set(names) - set(args)
    assert not missing, f"leak scan has no args for new tool(s): {missing}"

    responses = []
    for name in names:
        try:
            responses.append(cli.call(name, **args[name]))
        except MCPToolError as exc:            # tool-level errors are fine — scan them too
            responses.append({"code": exc.code, "message": exc.message})
    blob = json.dumps(responses)

    for forbidden in FORBIDDEN:
        assert forbidden not in blob, f"hidden channel state leaked through a tool: {forbidden!r}"

    # Also scan the JSON-RPC transcript the server would write to disk (a leak could hide there).
    srv2 = StdioMCPServer(_service(tmp_path), log_path=tmp_path / "rpc.jsonl")
    c2 = InProcessClient(srv2)
    c2.call("build_flowgraph", spec=spec)
    c2.call("run_flowgraph", n_bits=8000)
    c2.call("get_metrics")
    transcript = (tmp_path / "rpc.jsonl").read_text()
    for forbidden in ("clean_es_n0_db", "effective_es_n0_db", str(CLEAN_ES_N0)):
        assert forbidden not in transcript, f"hidden state leaked into the transcript: {forbidden!r}"


def test_exported_flowgraph_file_does_not_disclose_the_hidden_channel(tmp_path):
    """The leak scan above inspects tool RESPONSES. ``export_flowgraph`` also writes a file, and
    that file is an artefact the agent can ask for and read, so it needs the same guarantee.

    The risk is specific: the channel's noise amplitude is computed from the hidden channel
    quality, so writing the true value would disclose by arithmetic exactly what every tool
    response is careful not to state.
    """
    svc = _service(tmp_path)
    cli = InProcessClient(StdioMCPServer(svc))
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("qpsk").to_dict())
    out = cli.call("export_flowgraph", path=str(tmp_path / "probe.grc"))

    text = Path(out["path"]).read_text()
    # The hidden quality itself, and the noise amplitude that would reveal it.
    assert str(CLEAN_ES_N0) not in text
    assert "es_n0" not in text.lower()
    true_amplitude = f"{10 ** (-CLEAN_ES_N0 / 20.0):.4f}"[:5]
    assert true_amplitude not in text


def test_no_prompt_in_the_library_leaks_hidden_channel_state(tmp_path):
    """The scan above drives every TOOL. Prompts are a second surface reaching the agent, and a
    newer one: free text, handed over verbatim, never passing through a tool response. A single
    sentence naming the channel condition would end the project's central claim, and nothing else
    in this suite reads these strings.

    Driven against a service seeded with the same sentinel hidden state, and rendered with
    arguments as well as defaults, since an argument is substituted into the body.
    """
    from gr_autopilot.tools import prompts as prompt_library

    rendered = []
    for prompt in prompt_library.PROMPTS:
        rendered.append(prompt.render())
        for arg in prompt.arguments:
            rendered.append(prompt.render({arg.name: "qpsk"}))
    # The protocol payloads too, not just the bodies: titles and descriptions also reach a client.
    cli = InProcessClient(StdioMCPServer(_service(tmp_path)))
    rendered.append(json.dumps(prompt_library.list_prompts()))
    rendered.append(json.dumps(prompt_library.catalog()))
    blob = " ".join(rendered)

    for forbidden in FORBIDDEN:
        assert forbidden not in blob, f"a prompt leaks hidden channel state: {forbidden!r}"
    # The values themselves, at full precision and at the roundings a writer would plausibly use.
    # Only DISTINCTIVE forms are checked: rounding the reaction latency to one decimal gives
    # "0.0", which collides with ordinary target error ratios such as 0.01 and would fail on text
    # that discloses nothing.
    for value in (CLEAN_ES_N0, JAM_INR, JAM_REACTION):
        assert str(value) not in blob
    for value in (CLEAN_ES_N0, JAM_INR):
        for form in (f"{value:.1f}", f"{value:.2f}", str(round(value))):
            assert form not in blob, f"a prompt contains the hidden value {form!r}"
    assert cli.list_tools()          # the service is live; the prompts simply never consult it
