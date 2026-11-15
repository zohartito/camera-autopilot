"""Exercise the trusted guard against disposable git trees, never head code."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/test_ratchet.py"
WORKFLOW = ROOT / ".github/workflows/test-ratchet.yml"
TEST = "def test_kept():\n    assert True\n"
SECOND_TEST = "def test_removed():\n    assert 2 + 2 == 4\n"
BASE_REF = "${{ github.event.pull_request.base.sha }}"
HEAD_REF = "${{ github.event.pull_request.head.sha }}"
PR_NUMBER = "${{ github.event.pull_request.number }}"
FETCH = f'git fetch --no-tags origin "pull/{PR_NUMBER}/head"'
COMPARE = f'python3 -I scripts/test_ratchet.py --base "{BASE_REF}" --head "{HEAD_REF}"'


def eval_set():
    return {
        "clamp_bounds": {"low": 60, "high": 180},
        "deterministic": [{"id": "clamp", "expect": {"target_brightness": 60}}],
        "model_graded": [{"id": "warm", "brightness_range": [80, 140], "wb_allowed": ["warm"]}],
    }


class Repo:
    def __init__(self, root):
        self.root = root
        self.git("init", "-q", "--initial-branch=main", "--template=")

    def git(self, *args):
        result = subprocess.run(
            ["git", "-c", "user.name=Ratchet", "-c", "user.email=ratchet",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.root, text=True, capture_output=True, check=True,
        )
        return result.stdout.strip()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def write_eval(self, data, path="tests/eval_set.json"):
        self.write(path, json.dumps(data))

    def commit(self, message="fixture change"):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run(self, base=None, head=None):
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--base", base or self.base,
             "--head", head or self.git("rev-parse", "HEAD")],
            cwd=self.root, text=True, capture_output=True, timeout=30,
            env={**os.environ, "CAMERA_ENGINE": "mock"},
        )


@pytest.fixture
def repo(tmp_path):
    repo = Repo(tmp_path)
    repo.write("tests/test_sample.py", TEST + SECOND_TEST)
    repo.write("backend/test_backend.py", "def test_backend():\n    assert True\n")
    repo.write_eval(eval_set())
    repo.write("scripts/test_safe.sh", "#!/bin/sh\npython3 -m pytest backend tests\n")
    repo.write("scripts/eval_gate.sh", "#!/bin/sh\npython3 tests/eval_intent.py\n")
    repo.write("scripts/test_ratchet.py", SCRIPT.read_text())
    repo.write(".github/workflows/test-ratchet.yml", WORKFLOW.read_text())
    repo.write(".github/workflows/ci.yml", "steps:\n  - run: python3 scripts/test_ratchet.py\n")
    repo.base = repo.commit("fixture baseline")
    return repo


def fails_with(result, *kinds):
    assert result.returncode == 1, result.stdout + result.stderr
    actual = {line.split(" | ")[0] for line in result.stdout.splitlines()}
    assert set(kinds) <= actual, result.stdout
    assert "inspection_error" not in actual, result.stdout


def test_unchanged_suite_passes(repo):
    repo.commit()
    result = repo.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.endswith("no findings\n")


def test_removing_function_fails(repo):
    repo.write("tests/test_sample.py", TEST)
    repo.commit()
    fails_with(repo.run(), "removed_test")


@pytest.mark.parametrize("path", ["tests/test_sample.py", "backend/test_backend.py", "tests/eval_set.json"])
def test_deleting_test_file_fails(repo, path):
    (repo.root / path).unlink()
    repo.commit()
    fails_with(repo.run(), "deleted_test_file")


@pytest.mark.parametrize("source", [
    "import pytest\n@pytest.mark.skip\n" + TEST,
    "import pytest\n@pytest.mark.skipif(True, reason='fixture')\n" + TEST,
    "import pytest\n@pytest.mark.xfail(reason='fixture')\n" + TEST,
    "import pytest\ndef test_kept():\n    pytest.skip('fixture')\n",
    "import pytest\ndef test_kept():\n    pytest.xfail('fixture')\n",
    "import pytest\npytestmark = pytest.mark.skip(reason='fixture')\n" + TEST,
    "import pytest as pt\n@pt.mark.skip\n" + TEST,
    "from pytest import mark as m\n@m.xfail\n" + TEST,
    "from pytest import skip as omit\ndef test_kept():\n    omit('fixture')\n",
])
def test_new_skip_fails(repo, source):
    repo.write("tests/test_sample.py", source + SECOND_TEST)
    repo.commit()
    fails_with(repo.run(), "new_skip")


def test_changed_skip_condition_fails(repo):
    repo.write("tests/test_sample.py", "import pytest\n@pytest.mark.skipif(False, reason='fixture')\n" + TEST + SECOND_TEST)
    base = repo.commit()
    repo.write("tests/test_sample.py", "import pytest\n@pytest.mark.skipif(True, reason='fixture')\n" + TEST + SECOND_TEST)
    repo.commit()
    fails_with(repo.run(base=base), "new_skip")


def test_skip_words_in_comments_and_strings_are_data(repo):
    repo.write("tests/test_sample.py", '# pytest.skip("example")\nexample = "@pytest.mark.xfail"\n' + TEST + SECOND_TEST)
    repo.commit()
    assert repo.run().returncode == 0


@pytest.mark.parametrize("field,value", [
    ("brightness_range", [79, 140]), ("brightness_range", [80, 141]),
    ("wb_allowed", ["warm", "cool"]), ("wb_allowed", ["warm", None]),
])
def test_widened_case_range_or_bucket_fails(repo, field, value):
    data = eval_set()
    data["model_graded"][0][field] = value
    repo.write_eval(data)
    repo.commit()
    fails_with(repo.run(), "eval_range_widened")


@pytest.mark.parametrize("field,value", [("low", 59), ("high", 181)])
def test_widened_clamp_fails(repo, field, value):
    data = eval_set()
    data["clamp_bounds"][field] = value
    repo.write_eval(data)
    repo.commit()
    fails_with(repo.run(), "eval_range_widened")


@pytest.mark.parametrize("field", ["brightness_range", "wb_allowed", "clamp_bounds"])
def test_removed_range_fails(repo, field):
    data = eval_set()
    target = data if field == "clamp_bounds" else data["model_graded"][0]
    del target[field]
    repo.write_eval(data)
    repo.commit()
    fails_with(repo.run(), "eval_range_widened")


@pytest.mark.parametrize("section", ["deterministic", "model_graded"])
def test_dropped_case_fails(repo, section):
    data = eval_set()
    data[section] = []
    repo.write_eval(data)
    repo.commit()
    fails_with(repo.run(), "eval_cases_dropped")


def test_same_count_replacement_and_demotion_fail(repo):
    data = eval_set()
    data["watch"] = data["model_graded"]
    data["model_graded"] = [{**data["watch"][0], "id": "replacement"}]
    repo.write_eval(data)
    repo.commit()
    fails_with(repo.run(), "eval_cases_dropped")


@pytest.mark.parametrize("mutation", ["drop", "delete", "widen", "add"])
def test_learned_cases_are_additive(repo, mutation):
    path = "tests/eval_set.learned.json"
    data = {"model_graded": eval_set()["model_graded"], "watch": [{"id": "watch-one"}]}
    repo.write_eval(data, path)
    base = repo.commit()
    if mutation == "delete":
        (repo.root / path).unlink()
    else:
        if mutation == "drop":
            data["watch"] = []
        elif mutation == "widen":
            data["model_graded"][0]["brightness_range"] = [60, 180]
        else:
            data["watch"].append({"id": "watch-two"})
        repo.write_eval(data, path)
    repo.commit()
    result = repo.run(base=base)
    if mutation == "add":
        assert result.returncode == 0, result.stdout
    else:
        fails_with(result, "eval_range_widened" if mutation == "widen" else "eval_cases_dropped")


def test_additions_and_tighter_ranges_pass(repo):
    data = eval_set()
    data["model_graded"][0]["brightness_range"] = [90, 130]
    data["deterministic"].append({"id": "new-case"})
    repo.write_eval(data)
    repo.write("tests/test_sample.py", TEST + SECOND_TEST + "def test_added():\n    assert True\n")
    repo.commit()
    assert repo.run().returncode == 0


@pytest.mark.parametrize("path", ["scripts/test_safe.sh", "scripts/eval_gate.sh"])
def test_removed_gate_command_fails(repo, path):
    repo.write(path, "#!/bin/sh\n# no commands\n")
    repo.commit()
    fails_with(repo.run(), "gate_command_removed")


def test_editing_ci_cannot_hide_removed_test(repo):
    repo.write(".github/workflows/ci.yml", "steps: []\n")
    repo.write("tests/test_sample.py", TEST)
    repo.commit()
    fails_with(repo.run(), "removed_test")


@pytest.mark.parametrize("path", ["scripts/test_ratchet.py", ".github/workflows/test-ratchet.yml"])
@pytest.mark.parametrize("mutation", ["edit", "delete", "mode"])
def test_guard_changes_fail(repo, path, mutation):
    if mutation == "delete":
        (repo.root / path).unlink()
    elif mutation == "mode":
        (repo.root / path).chmod(0o755)
    else:
        repo.write(path, "# changed guard\n")
    repo.commit()
    fails_with(repo.run(), "guard_changed")


def test_approval_claim_does_not_bypass_guard(repo):
    repo.write("tests/test_sample.py", TEST)
    repo.commit("Tests-Reviewed-By: Zohar")
    fails_with(repo.run(), "removed_test")


def test_head_files_are_never_executed(repo):
    payload = "from pathlib import Path\nPath('HEAD_EXECUTED').touch()\nraise RuntimeError('head executed')\n"
    for path in ("scripts/test_ratchet.py", "conftest.py", "sitecustomize.py", "scripts/ast.py"):
        repo.write(path, payload)
    repo.write("tests/test_sample.py", payload + TEST)
    repo.commit()
    fails_with(repo.run(), "guard_changed", "removed_test")
    assert not (repo.root / "HEAD_EXECUTED").exists()


def test_working_tree_contents_are_ignored(repo):
    repo.write("tests/test_sample.py", "raise RuntimeError('uncommitted')\n")
    assert repo.run().returncode == 0


def test_moved_function_is_not_gone_everywhere(repo):
    repo.write("tests/test_sample.py", TEST)
    repo.write("tests/test_moved.py", SECOND_TEST)
    repo.commit()
    assert repo.run().returncode == 0


def test_async_and_class_methods_are_detected(repo):
    repo.write("tests/test_sample.py", TEST + "class TestGroup:\n    async def test_async(self):\n        assert True\n")
    base = repo.commit()
    repo.write("tests/test_sample.py", TEST)
    repo.commit()
    fails_with(repo.run(base=base), "removed_test")


@pytest.mark.parametrize("path,text", [
    ("tests/test_sample.py", "def broken("),
    ("tests/eval_set.json", "{broken"),
    ("tests/eval_set.json", '{"model_graded": [], "model_graded": []}'),
    ("tests/eval_set.json", '{"model_graded": [{"id": "same"}, {"id": "same"}]}'),
    ("tests/eval_set.json", '{"clamp_bounds": {"low": 60, "high": NaN}}'),
    ("tests/eval_set.json", '{"model_graded": [{"id": "warm", "brightness_range": "wide"}]}'),
])
def test_unparseable_data_fails_closed(repo, path, text):
    repo.write(path, text)
    repo.commit()
    result = repo.run()
    assert result.returncode == 2
    assert "inspection_error |" in result.stdout


def test_symlinks_are_not_followed(repo):
    target = repo.root / "tests/test_sample.py"
    target.unlink()
    target.symlink_to("../outside.py")
    repo.commit()
    result = repo.run()
    assert result.returncode == 2
    assert "inspection_error | tests/test_sample.py" in result.stdout


def test_invalid_revision_fails_closed(repo):
    result = repo.run(head="0" * 40)
    assert result.returncode == 2
    assert "inspection_error" in result.stdout


def test_second_binding_of_an_alias_cannot_hide_a_skip(repo):
    # `p` is pytest at module level and something else inside a helper.
    repo.write("tests/test_sample.py",
               "import pytest as p\n\n@p.mark.skip\ndef test_kept():\n    assert True\n\n"
               + SECOND_TEST
               + "def helper():\n    from pytest import fixture as p\n    return p\n")
    repo.commit()
    fails_with(repo.run(), "new_skip")


def test_alias_resolves_in_its_own_scope(repo):
    # One helper binds `p` to skip; other tests bind the same name to fixture and raises.
    base = ("def unused():\n    from pytest import skip as p\n\n" + TEST + SECOND_TEST)
    repo.write("tests/test_sample.py", base)
    repo.base = repo.commit("helper binds p to skip")
    repo.write("tests/test_sample.py", base
               + "def test_fixture():\n    from pytest import fixture as p\n    @p\n    def value():\n"
                 "        return 1\n    assert value.__name__ == 'value'\n\n"
               + "def test_raises():\n    from pytest import raises as p\n    with p(ValueError):\n"
                 "        raise ValueError()\n")
    repo.commit()
    result = repo.run()
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("source", [
    # Decorators are evaluated outside the function they decorate.
    "import pytest as p\n\n@p.mark.skip\ndef test_kept():\n    from pytest import fixture as p\n    assert p\n",
    # A class-body alias decorates a method.
    "class TestGroup:\n    from pytest import mark as m\n\n    @m.skip\n    def test_kept(self):\n        assert True\n",
    # A function-local alias is called in that function.
    "def test_kept():\n    from pytest import skip as omit\n    omit('fixture')\n",
])
def test_scoped_alias_skips_fail(repo, source):
    repo.write("tests/test_sample.py", source + SECOND_TEST)
    repo.commit()
    fails_with(repo.run(), "new_skip")


def test_relative_import_named_pytest_is_not_pytest(repo):
    repo.write("tests/test_sample.py", TEST + SECOND_TEST
               + "from .pytest import skip as p\n\ndef test_uses_local_helper():\n    p()\n")
    repo.commit()
    assert repo.run().returncode == 0


def test_deleting_one_of_two_same_named_tests_fails(repo):
    repo.write("tests/test_other.py", TEST)
    repo.base = repo.commit("two files define test_kept")
    repo.write("tests/test_other.py", "def test_different():\n    assert True\n")
    repo.commit()
    result = repo.run()
    fails_with(result, "removed_test")
    assert "test_kept (1 fewer definitions)" in result.stdout


def test_base_only_additions_do_not_look_like_pr_deletions(repo):
    # Make diverging commits without switching this temporary checkout.
    repo.write("tests/test_base_only.py", "def test_base_only():\n    assert True\n")
    newer_base = repo.commit()
    head_tree = repo.git("rev-parse", f"{repo.base}^{{tree}}")
    head = repo.git("commit-tree", head_tree, "-p", repo.base, "-m", "PR branch")
    assert repo.run(base=newer_base, head=head).returncode == 0


def assert_workflow_policy(workflow):
    # A closed allowlist is deliberate: new execution paths require review.
    assert set(workflow) == {"name", "on", "permissions", "jobs"}
    assert workflow["on"] == {"pull_request_target": {"types": ["opened", "synchronize", "reopened", "edited"]}}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"test-ratchet"}
    job = workflow["jobs"]["test-ratchet"]
    assert set(job) == {"runs-on", "timeout-minutes", "env", "steps"}
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 10
    assert job["env"] == {"CAMERA_ENGINE": "mock"}
    checkout, fetch, compare = job["steps"]
    assert set(checkout) == {"name", "uses", "with"}
    assert checkout["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert checkout["with"] == {
        "ref": BASE_REF, "fetch-depth": 0, "persist-credentials": False,
        "submodules": False, "lfs": False,
    }
    assert set(fetch) == set(compare) == {"name", "run"}
    assert fetch["run"] == FETCH
    assert compare["run"] == COMPARE
    serialized = json.dumps(workflow)
    assert serialized.count(HEAD_REF) == 1
    assert "secrets." not in serialized


def test_workflow_uses_only_trusted_base_code():
    assert_workflow_policy(yaml.safe_load(WORKFLOW.read_text()))


@pytest.mark.parametrize("attack", ["trigger", "head_checkout", "write_permission", "head_exec", "continue_on_error", "skip_job", "install", "environment"])
def test_workflow_policy_rejects_unsafe_variants(attack):
    workflow = deepcopy(yaml.safe_load(WORKFLOW.read_text()))
    job = workflow["jobs"]["test-ratchet"]
    if attack == "trigger":
        workflow["on"]["pull_request"] = None
    elif attack == "head_checkout":
        job["steps"][0]["with"]["ref"] = HEAD_REF
    elif attack == "write_permission":
        workflow["permissions"]["contents"] = "write"
    elif attack == "head_exec":
        job["steps"][1]["run"] += f'\ngit show "{HEAD_REF}:attack.py" | python3'
    elif attack == "continue_on_error":
        job["steps"][2]["continue-on-error"] = True
    elif attack == "skip_job":
        job["if"] = False
    elif attack == "install":
        job["steps"][2]["run"] = "pip install -r requirements.txt\n" + COMPARE
    else:
        job["steps"][2]["env"] = {"PYTHONPATH": "head"}
    with pytest.raises(AssertionError):
        assert_workflow_policy(workflow)
