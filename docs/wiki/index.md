# Introduction

This system lets a language model conduct radio experiments: it designs a signal-processing
chain, transmits it over a channel that may be simulated or made of real hardware, measures how
well it worked, and adjusts. A person can watch that happen live, take away the resulting
flowgraph, edit it, and hand it back.

This page explains what the system is for and how the pieces fit together. It assumes you have
met digital radio before but not this project. Every abbreviation is defined where it first
appears, and collected on the [notation page](/wiki/glossary).

## The problem this addresses

Building a digital radio link is a design task with many small decisions: which modulation to
use, how much bandwidth to spend on filter roll-off, whether the receiver needs carrier recovery,
how to react when someone else starts transmitting on your frequency. Each decision is informed by
measurements, and the measurements depend on the decisions. In practice an engineer iterates:
build, measure, look at the constellation, change one thing, measure again.

That loop is a reasonable thing to ask a machine to run. The difficulty is not the arithmetic —
it is that the loop requires *judgement about what to try next given what was just measured*, and
that judgement has historically been the engineer's.

Two things make an honest attempt at this possible now. Language models can carry out that kind of
iterative reasoning when given tools and feedback. And GNU Radio provides a large, well-tested
library of signal-processing blocks, so an agent composing a chain is assembling real components
rather than inventing signal processing from scratch.

## What the system actually does

An agent connects to a server, is offered a set of tools, and is given a goal such as *keep the
bit error ratio below one in a hundred while sending as many bits per symbol as possible*. It is
**not** told the quality of the channel. It has to discover that by measuring, exactly as an
engineer with an unfamiliar link would.

A typical session looks like this:

0. The agent asks whether an experiment is selected. If not, it asks *you* what to call it —
   the name becomes the directory every result is written to — and nothing can be measured
   until that is answered.
1. The agent asks what signal-processing blocks are available and what radios are attached.
2. It composes a transmit chain and a receive chain and builds a flowgraph.
3. It runs the flowgraph. The framework transmits a payload it already knows, receives it, and
   counts the errors.
4. The agent reads the measurements, looks at the constellation, and decides what to change.
5. It repeats until the goal is met, recording its reasoning as it goes.

Everything in that loop is observable while it happens, and everything is recorded afterwards.

## Three commitments

Three design decisions shape the rest of this documentation, and they are worth stating before
the details.

**The chain the agent writes is the chain that runs.** When the agent lists
`rrc_matched_filter` in its receive chain, a real matched-filter block is placed in a real GNU
Radio flowgraph. Removing it makes the measurement worse, and removing the symbol synchroniser
makes the link fail outright. The framework does not quietly supply a stage the agent omitted,
because doing so would conceal the very failure the agent needs to observe. See
[signal-processing chains](/wiki/flowgraphs).

**The framework grades; the agent measures.** The framework owns the transmitted payload, so it
can count errors against the truth. It never tells the agent the channel condition, the presence
of an interferer, or which answer is correct. What the agent knows, it learned by measuring. See
[measurement integrity](/wiki/integrity).

**A measurement that did not happen is never recorded.** If a radio is disconnected mid-run, the
receiver still returns buffers, and those buffers grade as an error ratio close to one half —
indistinguishable from severe interference. Recording that would put a fabricated result in the
history and teach the agent that the channel degraded when in fact a cable came out. Instead the
iteration is recorded as aborted, with no measurement attached. See
[radios and device health](/wiki/devices).

## What runs where

The system is one long-lived local server, `autopilotd`, which serves four things on one port:
the tool endpoint an agent connects to, the live dashboard, the experiment records, and this
documentation.

Radios are deliberately **not** held by that server. They are owned by a separate short-lived
process, because losing a radio does not fail gracefully — the underlying input/output library
terminates whichever process is holding the device. Keeping radios at arm's length is what allows
the server to stay up while a device comes and goes. This is explained in full under
[system architecture](/wiki/architecture).

## Where to go next

- To run it: [getting started](/wiki/getting-started).
- To understand the parts: [system architecture](/wiki/architecture).
- To try something specific without inventing an experiment yourself: [the experiment
  library](/wiki/library) contains ready-made exercises, from *name the experiment* and
  *build your first link* to *find how high the ladder goes* and *out-run a jammer that
  follows you*.
- To know what this system does not do: [limitations](/wiki/limitations). That page is not a
  formality; several of the entries in it are load-bearing.

!!! warning "Research use"
    This is laboratory equipment for research. Transmitting requires the appropriate authority
    for the frequency, power and location in question, and the interference experiments described
    here are intended for a contained path — cabled and attenuated, or a **shielded** enclosure —
    where nothing reaches open spectrum. *Anechoic* is not *shielded*: absorber suppresses
    reflections and does nothing to contain emission. The jammer will not arm unless the bench
    declaration attests to containment. See `RESPONSIBLE-USE.md` in the repository.
