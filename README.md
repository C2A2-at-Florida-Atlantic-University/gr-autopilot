# gr-autopilot: An Agentic Framework for Closed-Loop Flowgraph Synthesis with Hardware-in-the-Loop Feedback
To be presented at GRCon26, September 2026 [GRCon26 Page](https://events.gnuradio.org/)<br/>
Authors: Stepan Mazokha, Gabriel Garcia, George Sklivanitis (Florida Atlantic University)<br/>
Paper: coming soon<br/>
Presentation Recording: coming soon

## Brief Intro

The project consists of the following components:

* `gr_autopilot/daemon.py`: the long-running server. It serves the agent's MCP tools over HTTP at
  `/mcp`, the operator dashboard at `/`, the operator's own controls at `/control`, and the
  documentation at `/wiki`. A stdio server (`python -m gr_autopilot.mcp_server`) is also
  available, simulation only.
* `gr_autopilot/tools/`: the agent's tool surface. It builds flowgraphs, runs and measures them,
  senses the spectrum, retunes or hops, runs Bayesian optimization, and manages experiments.
* `gr_autopilot/flowgraph/`, `gr_autopilot/blocks/`: the GNU Radio block catalogue and the
  `.grc` compile-and-run path. Blocks that could escape the sandbox (e.g. `epy_block`) are refused
  by a deny-list.
* `gr_autopilot/link/`, `gr_autopilot/scoring/`: simulated and PlutoSDR links (Zadoff-Chu
  acquisition, pilot-aided carrier recovery, hopping) and the framework-owned BER/EVM/SNR grader.
* `gr_autopilot/control/`, `gr_autopilot/optimize/`: reference controllers for adaptive modulation
  and coding, avoidance, hopping and power, plus a small Gaussian-process Bayesian optimizer on
  numpy/scipy.
* `gr_autopilot/hardware/`: device detection over libiio, the bench topology check, the radio
  worker process, and the HackRF interferer.
* `dashboard/`: the React operator console. The built `dist/` is committed, so no Node is needed
  to run it.
* `config/bench.yaml`: the declaration of *our* bench. With `--radios`, the daemon checks each
  Pluto's serial, chip and tuning range against it.
* `scripts/`: reproduction scripts, in simulation and on hardware.

## Hardware & Software Requirements

To run `gr-autopilot`, you have the following requirements:

* **No radios** for simulation. With the software below installed, every tool, the dashboard and
  the test suite run on the host alone. GNU Radio executes the agent's flowgraphs in simulation.

* **Two [ADALM-Pluto](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/adalm-pluto.html) SDRs** for the hardware link, one
  transmitting and one receiving.

    **Note**: every Pluto ships at `192.168.2.1`, so move the receiver to its own subnet. Edit
    `ipaddr = 192.168.3.1` and `ipaddr_host = 192.168.3.10` in `config.txt` on its USB drive, then
    eject the drive. One of our units is modified to present as AD9364 (70 MHz–6 GHz). It is not
    needed for the 2.4 GHz experiments.

* **A [HackRF One](https://greatscottgadgets.com/hackrf/one/)** (optional), used as the
  framework-controlled interferer for the avoidance and hopping experiments.

* **A contained RF path**: either coax with attenuation between the radios (we used 20 dB), or a
  **screened** enclosure. An anechoic chamber that is not shielded does not contain emission.

* **Ubuntu 22.04 with the system Python 3.10**, `numpy` ≥ 1.21, `scipy` ≥ 1.8, `PyYAML` and
  `pytest`; **GNU Radio 3.10** with `gr-iio` (tested with 3.10.7); **libiio** with its
  command-line tools (`iio_info`, `iio_attr`); and `hackrf` tools for the interferer. Optional:
  `python3-markdown` renders the wiki, `matplotlib` draws figures, and Node.js is only needed to
  rebuild the dashboard.

    **Note**: do not `pip install` numpy or scipy on top of the system packages, because a newer
    wheel shadows the one GNU Radio was built against. Run everything with the interpreter that
    can `import gnuradio`, usually `/usr/bin/python3` rather than a conda Python.

* **An MCP client**, e.g. [Claude Code](https://claude.com/claude-code) or any client that supports
  MCP over HTTP.

## Instructions

1. Install the dependencies and get the code:

    ```bash
    sudo add-apt-repository ppa:gnuradio/gnuradio-releases
    sudo apt install gnuradio libiio-utils hackrf python3-numpy python3-scipy python3-yaml \
                     python3-pytest python3-markdown python3-matplotlib
    git clone https://github.com/C2A2-at-Florida-Atlantic-University/gr-autopilot.git && cd gr-autopilot
    /usr/bin/python3 -c "from gnuradio import gr; print(gr.version())"
    PYTHONPATH=. /usr/bin/python3 -m pytest -q      # no radios needed
    ```

2. Start the daemon. It runs in simulation until you pass `--radios`:

    ```bash
    PYTHONPATH=. /usr/bin/python3 -m gr_autopilot.daemon --port 8080 --runs-dir runs
    ```

    Open `http://127.0.0.1:8080/`. The dashboard asks you to name an experiment, which is stored
    under `runs/<slug>/` (e.g. "My QPSK Test" goes to `runs/my-qpsk-test/`). Until one is selected,
    the tools that build, run or record trials refuse to run. The **Operator page** is where you
    set the hidden channel and key the interferer; on the bench host it needs no credential, and
    from another machine it takes the same `--token` as `/mcp` (step 5).

    The documentation is at `http://127.0.0.1:8080/wiki`; the Markdown sources in `docs/wiki/` use
    links that only resolve there.

3. Connect an agent:

    ```bash
    claude mcp add --transport http gr-autopilot http://127.0.0.1:8080/mcp
    ```

    Then ask it for a link, e.g. *"Build a QPSK link and show me that it works."* The three
    experiments reported in the paper are at `/wiki/library` as ready-made prompts. For a graded
    run, give the agent only the MCP tools, not a shell on the bench host.

4. Move to the radios. The shipped [config/bench.yaml](config/bench.yaml) describes our radios,
    our calibration and our shielded chamber. Replace the `devices`, `common.calibration` and
    `attestation` sections with facts about your own bench. Then start with:

    ```bash
    PYTHONPATH=. /usr/bin/python3 -m gr_autopilot.daemon --port 8080 --runs-dir runs \
        --radios --tx-uri ip:192.168.2.1 --rx-uri ip:192.168.3.1 [--verify-path] [--jammer]
    curl -s http://127.0.0.1:8080/devices
    ```

    Check the banner's `backend` and `topology` lines. If a radio can't be reached, the daemon
    falls back to simulation. If a Pluto disagrees with `bench.yaml`, it reports a topology FAULT
    and refuses to run trials. `--verify-path` transmits a short BPSK burst at startup to confirm
    the transmitter reaches the receiver. If `--jammer` is also set, it keys the HackRF briefly
    too. `--jammer` makes the HackRF armable from the Operator page. It is refused unless
    `bench.yaml` declares a HackRF and attests a contained path: `medium: coax` with
    `antennas_attached: false`, or `medium: anechoic` with `shielded: true`. That attestation is the only interlock on the HackRF.

5. (Optional) Reach the dashboard from another machine. The daemon binds to loopback by default.
    Either tunnel (`ssh -N -L 8080:127.0.0.1:8080 user@bench-host`), or bind to the network with a
    bearer token:

    ```bash
    openssl rand -hex 16 > config/mcp.token          # git-ignored
    PYTHONPATH=. /usr/bin/python3 -m gr_autopilot.daemon --host 0.0.0.0 --port 8080 \
        --runs-dir runs --token "$(cat config/mcp.token)"
    ```

    Then browse to `http://<bench-host>.local:8080/`. A remote MCP client must send
    `Authorization: Bearer <token>`; clients on the bench host don't need it, because a local
    process could read the token out of the daemon's command line anyway. The same rule covers
    the write surface: naming an experiment and driving the Operator page from a remote browser
    need the token (both pages have a field for it), while a browser on the bench host does not.
    The read-only pages (`/devices`, `/topology`, `/data`, `/flowgraphs`) are open to anyone who
    can reach the port, so do this only on a network you trust.

    **One credential, and what that costs.** The token you hand a remote MCP client is the same
    one that opens `/control`, so an agent driven over the network can reach the operator surface.
    The framework's own tools never expose it — no tool posts to `/control`, and
    `tests/test_integrity_leak.py` checks that — but an agent given a shell
    on the bench host is not prevented by the transport. What is still guaranteed is the number
    that matters: `GET /control` does not report the hidden channel quality to anyone, the
    telemetry snapshot strips it, and every tool response is scanned for it. For a graded run,
    give the agent only the MCP tools.

6. Reproduce the results. Run scripts from the repository root with the same interpreter:

    ```bash
    PYTHONPATH=. /usr/bin/python3 scripts/run_reference_loop.py          # staircase rediscovery, sim
    PYTHONPATH=. /usr/bin/python3 scripts/run_hw_amc.py --sync zc        # staircase on the radios
    PYTHONPATH=. /usr/bin/python3 scripts/run_hw_hopping.py              # out-hop a following jammer
    PYTHONPATH=. /usr/bin/python3 scripts/run_hw_mission.py --with-hop   # the sustained mission
    ```