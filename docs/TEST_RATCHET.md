# Test ratchet

`scripts/test_ratchet.py` reports supported forms of test weakening between
committed trees. It uses only the Python standard library and Git. Findings
return exit 1; data that cannot be inspected (a bad revision, an unreadable or
malformed file) returns exit 2; no findings return exit 0. There is no approval flag, and commit
messages, PR text and labels are not consulted.

## Trust model

GitHub's [event documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_target),
checked 2026-11-15, states:

> This event runs in the context of the default branch of the base repository

The same documentation identifies the event SHA/ref with the default branch.
Thus the workflow definition is trusted default-branch code, not the PR's
proposed workflow. This is more precise than assuming the event always uses the
PR target branch. The [checkout documentation](https://github.com/actions/checkout#usage)
says its default ref follows the triggering event. Our workflow does not rely
on that default: it explicitly checks out `github.event.pull_request.base.sha`.
The target/base branch must therefore also be trusted.

The separate `test-ratchet` job:

1. Checks out that explicit base SHA with full history, using a pinned checkout
   action, no persisted credentials, submodules or LFS.
2. Fetches the public repository's `pull/<number>/head` objects without checking
   them out. No secret is referenced; the job token has only `contents: read`.
3. Runs `python3 -I scripts/test_ratchet.py` from the base checkout, passing the
   event's base and head SHAs. Python isolation prevents path/environment imports.

No dependency installation, local action, head import, head checkout or head
execution occurs. The exact head SHA from the event is inspected, not whatever
`FETCH_HEAD` happens to contain. If a force-push makes that object unavailable,
inspection fails instead of substituting another revision.

The job declares `CAMERA_ENGINE: mock`, matching the repository's workflow policy.
The workflow has no path filters, PR-controlled conditions or failure suppression.
It handles opened, reopened, synchronized and edited PRs (including base retargets).
Edits to `ci.yml` do not remove this separate check. Static tests parse the YAML
and allow only the intended checkout/fetch/compare steps and read-only permissions.

## What is compared

The guard calls `git diff --name-status base...head` with NUL-delimited paths,
rename detection and external diff/text conversion disabled. Baseline contents
come from the merge base, matching Git's three-dot PR diff; head contents come
from the requested commit. This avoids treating additions made only on the
base branch since divergence as PR deletions. `git ls-tree` inventories paths
and modes; `git show SHA:path` reads blobs. Head Python is parsed with `ast`,
never imported or executed. Uncommitted working-tree contents are irrelevant.

| Finding | Rule |
| --- | --- |
| `removed_test` | The number of definitions of a `test_*` function name, including async functions and methods, drops across all tracked Python files. Deleting one of two same-named tests is caught; moving a test between files is not a finding. |
| `deleted_test_file` | A tracked file under `tests/`, or a direct `backend/test_*.py` file, is deleted or renamed. |
| `new_skip` | New or changed pytest skip/skipif/xfail calls or marks, including bare marks, `pytestmark` and direct import aliases (every binding of a name in the file is considered). Comments and strings are data. |
| `eval_cases_dropped` | An existing `(section, id)` disappears from curated or tracked learned eval data, even if another case replaces it. Moving a graded case to `watch` also removes its old identity. |
| `eval_range_widened` | A lower bound decreases, an upper bound increases, an allowed bucket gains a member, or an existing range/bucket is removed. |
| `gate_command_removed` | A nonblank, noncomment line disappears from `scripts/test_safe.sh` or `scripts/eval_gate.sh`. Duplicate lines are counted. |
| `guard_changed` | The PR adds, edits, deletes, renames or changes the mode of the guard script or workflow. |
| `inspection_error` | Git objects are missing, protected inputs are not regular files, or inspected Python/JSON/ranges cannot be parsed. This also fails. |

The eval fields were checked against `tests/eval_intent.py`,
`tests/eval_set.json` and `tests/test_feedback_to_eval.py`: case identity is `id`
within `deterministic`, `model_graded` or `watch`; numeric bounds are global
`clamp_bounds.low/high` and each case's `brightness_range`; the bucket is
`wb_allowed`. Ranges require two ordered finite numbers. Duplicate JSON keys
and duplicate case IDs are inspection errors. Additions and tighter bounds
are permitted. The learned file is optional and gitignored in this public
repository; it is inspected only when it exists in a compared tree.

## Local verification

From `/path/to/repo`, with `requirements-test.lock` installed in `.venv`:

```sh
CAMERA_ENGINE=mock .venv/bin/python -m pytest -q tests/test_test_ratchet.py
bash scripts/test_safe.sh
scripts/test_ratchet.py --base "$(git rev-parse HEAD~5)" --head "$(git rev-parse HEAD)"
```

The focused tests use temporary Git repositories and run the trusted script
against their committed trees, including a head containing executable payloads.
PyYAML is required only by the static workflow test, not by the guard.

## Activation and limits

The initial change requires owner review and merge before the workflow can run
from the default branch. The bootstrap diff itself reports `guard_changed` for
the new guard files when checked locally. Later intentional guard edits also
fail; there is no automatic approval escape. A base branch without the script
fails to run it rather than falling back to a copy from the PR.

A failing job does not by itself prevent a repository owner from merging.
Requiring `test-ratchet` in repository rules is a separate owner decision. No
repository settings, rules or secrets are changed by this implementation.
Hosted workflow execution and any required-check enforcement must be verified
after activation; local tests establish neither.

These checks are structural heuristics. Keeping a function name while deleting
assertions, changing collection configuration, inserting an early exit into a
gate while retaining its lines, dynamic aliases and other semantic weakening
can escape them. The inventory counts names, not what pytest collects: renaming
a `Test*` class so its methods stop being collected is not detected, and a
`test_*` function in a non-test file is counted, so renaming such a helper is
reported. Non-standard JSON constants such as `NaN` outside the checked ranges
are accepted, and a tracked path that is not valid UTF-8 is an inspection
error. Function moves are allowed because definitions are counted across files;
test-file renames still produce a file-deletion finding. Gate formatting changes
and legitimate skip changes may need owner review. The guard does not replace
review, the camera-free suite, or real-camera receipts.
