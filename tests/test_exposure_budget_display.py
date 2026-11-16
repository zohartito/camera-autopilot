"""CAM-03: UI and CLI counts include the shared remeter allowance."""
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest
from test_final_wave_product_api import product  # noqa: F401
from test_final_wave_cli import cli  # noqa: F401
import capture_policy
import product_capture
import shoot

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("refine", [False, True])
def test_cli_announces_shared_physical_exposure_budget(cli, refine, monkeypatch, capsys):
    import remeter
    # Change the source allowance to catch a separately hard-coded display limit.
    monkeypatch.setattr(remeter, "MAX_REMETER_PASSES", 3)
    monkeypatch.setattr(sys, "argv", ["shoot.py"] + (["--auto-correct"] if refine else []))
    monkeypatch.setattr(product_capture, "shoot", lambda *a, **k: cli.result)
    budget = capture_policy.exposure_budget(capture_policy.validate_stack_policy(), refine=refine)
    assert shoot.main() == 0
    out = capsys.readouterr().out
    assert f"up to {budget['max_physical_exposures']} exposures" in out
    assert "meter" in out


def test_web_budget_display_uses_server_policy(product, monkeypatch):
    import remeter
    monkeypatch.setattr(remeter, "MAX_REMETER_PASSES", 3)
    monkeypatch.setattr(product.fake, "status", lambda: {"engine": "mock", "ready": True}, raising=False)
    response = product.client.get("/status", headers=product.headers)
    assert response.status_code == 200
    budgets = response.json()["capture_budgets"]
    for key, refine in (("ordinary", False), ("refined", True)):
        assert budgets[key] == capture_policy.exposure_budget(capture_policy.validate_stack_policy(), refine=refine)
    html = (ROOT / "backend/web/index.html").read_text()
    function = re.search(r"  function updateCaptureBudget\(\) \{.*?\n  \}", html, re.S).group()
    script = "const assert=require('node:assert/strict'); const window={};\n"
    script += (ROOT / "backend/web/workflows.js").read_text()
    script += "\nconst AutopilotWorkflows=window.AutopilotWorkflows;\n"
    script += "const captureBudgets=" + json.dumps(budgets) + ";\n"
    script += """
const elements = new Proxy({}, {get(o,k) {return o[k] ||= {value:'', checked:false, textContent:''};}});
const $=id=>elements[id]; const busy=false; let captureMode='photo';
const stackBaselineSource=()=> 'metered';
$('stack-policy').value='off'; $('stack-frames').value='3';
""" + function + """
updateCaptureBudget();
assert.match($('capture-count').textContent, /up to 5 photos/);
assert.match($('capture-count').textContent, /3 extra meter/);
assert.match($('refine-budget').textContent, /up to 10 exposures/);
$('refine').checked=true; updateCaptureBudget();
assert.match($('capture-count').textContent, /Up to 10 photos/);
$('stack-policy').value='auto'; updateCaptureBudget();
assert.match($('capture-count').textContent, /up to 8 physical photos/);
$('stack-policy').value='fixed'; updateCaptureBudget();
assert.match($('capture-count').textContent, /up to 7 physical photos/);
assert.match($('capture-count').textContent, /up to 3 more/);
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "captureBudgets = s.capture_budgets" in html
