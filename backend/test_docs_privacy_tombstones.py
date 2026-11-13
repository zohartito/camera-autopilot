"""Keep historical camera evidence from silently becoming current guidance."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_current_docs_have_no_deleted_embeds() -> None:
    # The public repository ships no progress screenshots or Playwright captures; current docs must
    # not point at them.
    for path in (ROOT / "README.md", ROOT / "docs" / "README.md"):
        text = path.read_text(encoding="utf-8")
        assert "docs/PROGRESS.md" not in text and "output/playwright/" not in text, path


def test_active_release_docs_do_not_claim_private_route3_evidence_is_current() -> None:
    release = (ROOT / "docs" / "SECURITY_PRIVACY_RELEASE.md").read_text(encoding="utf-8")
    assert "Route 3 is permanently" in release
    assert "no Route 3 operation is authorized" in release


if __name__ == "__main__":
    test_current_docs_have_no_deleted_embeds()
    test_active_release_docs_do_not_claim_private_route3_evidence_is_current()
    print("docs privacy tombstone tests: PASS")
