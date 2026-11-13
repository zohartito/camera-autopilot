"""Static and node checks for the Sessions panel's partial-movie button (owner decision 2026-10-02)."""
from pathlib import Path
import subprocess


WEB = Path(__file__).resolve().parents[1] / "backend/web"
HTML = (WEB / "index.html").read_text()
WORKFLOWS = (WEB / "workflows.js").read_text()


def run_js(code: str) -> None:
    subprocess.run(["node", "-e", "const assert=require('node:assert/strict');\n" + code],
                   check=True, capture_output=True, text=True)


def test_sessions_panel_declares_the_partial_movie_controls() -> None:
    for element_id in ["session-encode-partial", "session-encode-partial-note", "session-partial-movie"]:
        assert f'id="{element_id}"' in HTML, f"missing element id {element_id}"
    assert "The ambiguous frame is not used and nothing is shot" in HTML
    assert "'/sessions/'+id+'/encode-partial'" in HTML
    assert "AutopilotWorkflows.partialMovieOffer(state)" in HTML and "AutopilotWorkflows.partialMovieSummary(state.partial_video)" in HTML
    assert "selectedSessionState?.partial_video?.url" in HTML
    assert '<script src="/workflows.js?v=20261003c"></script>' in HTML


def test_partial_offer_only_for_a_blocked_timelapse_with_two_good_frames() -> None:
    run_js("const window={};\n" + WORKFLOWS + """
      const flows=window.AutopilotWorkflows;
      const frames=n=>[...Array(n).keys()].map(i=>({index:i,status:'accepted'})).concat([{index:n,status:'ambiguous'}]);
      const blocked={mode:'timelapse',status:'needs_reconciliation',frames:frames(13)};
      assert.deepEqual(flows.partialMovieOffer(blocked), {count:13});
      for (const status of ['completed','running','identity_refused','failed','cancelled','ready'])
        assert.equal(flows.partialMovieOffer({...blocked,status}), null);
      assert.equal(flows.partialMovieOffer({...blocked,mode:'hdr'}), null);
      assert.equal(flows.partialMovieOffer({...blocked,frames:frames(1)}), null);
      for (const status of ['completed','queued','running'])
        assert.equal(flows.partialMovieOffer({...blocked,partial_video:{status}}), null);
      // A failed or unavailable attempt may be tried again.
      assert.deepEqual(flows.partialMovieOffer({...blocked,partial_video:{status:'failed'}}), {count:13});
      assert.deepEqual(flows.partialMovieOffer({...blocked,partial_video:{status:'unavailable'}}), {count:13});
      assert.equal(flows.partialMovieOffer(null), null);

      assert.match(flows.partialMovieSummary({status:'completed',frame_count:13}),
                   /13 good frames; the ambiguous frame is left out and nothing was shot/);
      assert.match(flows.partialMovieSummary({status:'queued'}), /no camera is used/);
      assert.match(flows.partialMovieSummary({status:'unavailable',reason:'ffmpeg missing.'}), /saved frames remain/);
      assert.match(flows.partialMovieSummary({status:'failed',error:'boom'}), /try again/);
      assert.equal(flows.partialMovieSummary(null), '');
    """)
