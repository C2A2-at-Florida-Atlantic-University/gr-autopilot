"""The dependency-free implementations must agree with the library ones.

Several modules here deliberately reimplement things GNU Radio provides, because the runtime is
dependency-free by design: the framework, its grader and its simulator must run and be testable on
a host with numpy and nothing else. That is a good reason to have a second implementation, and no
reason at all for the two to disagree.

So the library is used as an ORACLE rather than as a replacement. These tests fail the moment the
hand-written version drifts from the reference, which is the actual risk of keeping both: the
hardware path shapes its pulses with ``firdes`` while the numpy path uses ``sync.rrc_taps``, and a
divergence there would make the reference model quietly stop describing the bench it is supposed to
predict.

Marked ``gnuradio`` so they are skipped where the library is absent -- and note that a run which
skips them is reported at the end and can be made fatal with GR_AUTOPILOT_REQUIRE_GNURADIO=1.
"""
import numpy as np
import pytest

pytest.importorskip("gnuradio")
pytestmark = pytest.mark.gnuradio


@pytest.mark.parametrize("sps,rolloff,span", [(4, 0.35, 8), (8, 0.35, 8), (4, 0.5, 8), (8, 0.2, 8)])
def test_rrc_taps_match_firdes(sps, rolloff, span):
    """The two pulse shapes on the bench must be the same pulse shape.

    ``link/pluto.py`` filters with ``firdes.root_raised_cosine``; ``link/sync.py`` computes its own
    taps for the numpy path. If these drift, the simulator stops predicting the radios and the
    disagreement shows up as an unexplained BER gap rather than as a filter bug.
    """
    from gnuradio.filter import firdes

    from gr_autopilot.link import sync

    ours = np.asarray(sync.rrc_taps(sps, rolloff, span), dtype=float)
    theirs = np.asarray(firdes.root_raised_cosine(1.0, float(sps), 1.0, rolloff, ours.size),
                        dtype=float)
    a = ours / np.linalg.norm(ours)
    b = theirs / np.linalg.norm(theirs)
    assert np.allclose(a, b, atol=1e-6), f"max deviation {np.max(np.abs(a - b)):.2e}"


@pytest.mark.parametrize("mod,gr_name", [("bpsk", "constellation_bpsk"),
                                         ("qpsk", "constellation_qpsk")])
def test_constellations_match_gnuradio(mod, gr_name):
    """Same points, same energy. Bit LABELS are allowed to differ -- the framework grades against
    its own mapping consistently on both sides -- but the geometry must not."""
    from gnuradio import digital

    from gr_autopilot import modulation

    ours = np.asarray(modulation.get(mod).points, dtype=complex)
    theirs = np.asarray(getattr(digital, gr_name)().points(), dtype=complex)
    assert ours.size == theirs.size
    # Compare as unordered point sets at matched average energy.
    ours_n = ours / np.sqrt(np.mean(np.abs(ours) ** 2))
    theirs_n = theirs / np.sqrt(np.mean(np.abs(theirs) ** 2))
    for p in ours_n:
        assert np.min(np.abs(theirs_n - p)) < 1e-6, f"{p} is not a {gr_name} point"


def test_16qam_is_a_square_grid_with_the_symmetry_gnuradio_reports():
    """The 4-fold rotational symmetry is the reason coded 16-QAM needs pilots or differential
    encoding: it is what makes four carrier phases equally valid. GNU Radio reports it; the
    framework's own constellation must actually have it."""
    from gnuradio import digital

    from gr_autopilot import modulation

    assert digital.constellation_16qam().rotational_symmetry() == 4

    pts = np.asarray(modulation.get("16qam").points, dtype=complex)
    rotated = pts * 1j                                    # 90 degrees
    for p in rotated:
        assert np.min(np.abs(pts - p)) < 1e-9, "rotating 90 degrees must map the set onto itself"


def test_differential_quadrant_coding_is_rotation_invariant():
    """GNU Radio's differential blocks, used the way the fix would use them.

    ``diff_encoder_bb``/``diff_decoder_bb`` carry a symbol index as the CHANGE from the previous
    one, so a constant offset added on the wire -- which is exactly what a 90-degree carrier slip
    does to the quadrant index -- cancels in the difference. This is the property the hand-written
    alternative would have had to reproduce, and it is already a tested C++ block.
    """
    from gnuradio import blocks, digital, gr

    def run(data, block):
        tb = gr.top_block()
        src = blocks.vector_source_b([int(v) for v in data], False)
        snk = blocks.vector_sink_b()
        tb.connect(src, block, snk)
        tb.run()
        return np.asarray(snk.data(), dtype=int)

    M = digital.constellation_16qam().rotational_symmetry()    # 4, not hardcoded
    rng = np.random.default_rng(0)
    quad = rng.integers(0, M, 1000)

    wire = run(quad, digital.diff_encoder_bb(M))
    assert np.array_equal(wire, np.cumsum(quad) % M)

    for slip in range(M):
        recovered = run((wire + slip) % M, digital.diff_decoder_bb(M))
        # The first symbol absorbs the offset; everything after it is exact.
        assert np.array_equal(recovered[1:], quad[1:]), f"slip of {slip} quadrants was not cancelled"
