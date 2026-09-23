# The experiment library

The library is the three experiments reported in the paper, each one a ready-made prompt and
nothing else: the modulation staircase, the fixed jammer, and the following jammer.

## Prompts, not fixtures

There is no saved flowgraph behind any of these, no script, no prepared data. Each is text, and
the agent has to carry it out through the same published tools anyone else would use.

That constraint is deliberate. If an exercise here cannot be completed with the published tools,
then the tools are incomplete — and it shows up as a failed exercise rather than being concealed
by a prepared asset.

## Using them

A client supporting the Model Context Protocol's prompt facility will offer them as a menu.
Directly:

```bash
curl -s -X POST http://127.0.0.1:8080/mcp -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":1,"method":"prompts/list"}'
```

Or read them in a browser at `/prompts`.

Each takes arguments — the error-ratio target, the number of hop channels, the number of
baseline trials — so the same experiment can be re-run at a different operating point without
rewriting it. All three are pinned to the radios: each one's result is a claim about what
hardware does, and the same text run against a model would read identically while measuring
something else, so the venue is not the caller's to pick.

## The exercises

Everything below is generated from the catalogue the agent is offered, so it is the library —
not a description of it. Each entry shows the prompt exactly as the agent receives it with default
arguments, and an **On the bench** note for the operator: what to arm, which number to trust,
which caveat applies today. The note is the one thing on this page the agent is never sent.

<!-- catalog -->

## How they are written

Three rules, each enforced by a test.

**No prompt states the answer.** Nothing here names the channel condition, asserts that an
interferer exists, or says which modulation will win. The countermeasure exercises describe a
symptom and leave the diagnosis to the agent. This is the same discipline as the rest of the
[integrity](/wiki/integrity) work, applied to a surface that no response-based guard would ever
inspect.

**Every prompt asks for something to be reported.** An exercise producing no stated finding
teaches nothing.

**Every tool a prompt names must exist.** A prompt instructing the agent to call something
unpublished would send it looking for a tool that is not there, and signals that the library and
the tool surface have drifted apart.
