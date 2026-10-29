from galgame_news.supplement.ledger import SupplementLedger, normalize_url


def test_url_identity_keeps_meaningful_query_and_fragment_but_drops_tracking():
    assert normalize_url("HTTPS://Example.com/a?lang=ja&utm_source=x#gallery") == \
        "https://example.com/a?lang=ja#gallery"
    assert normalize_url("https://example.com/a?id=1") != normalize_url("https://example.com/a?id=2")
    assert normalize_url("https://example.com/a#gallery") != normalize_url("https://example.com/a#graphic")


def test_ledger_tracks_terminal_states_redirect_aliases_and_failed_retry(tmp_path):
    ledger = SupplementLedger(tmp_path)
    source = "https://example.com/product?sku=7&utm_medium=mail#gallery"
    canonical = "https://cdn.example.com/item?sku=7#gallery"
    ledger.set_state("n1", source, "completed", redirect_url=canonical)
    assert ledger.get("n1", source)["state"] == "completed"
    assert ledger.resolve("n1", source) == canonical
    assert ledger.should_skip("n1", canonical)
    assert not ledger.should_skip("n2", canonical)
    ledger.set_state("n1", "https://example.com/fail", "failed")
    assert ledger.should_skip("n1", "https://example.com/fail")
    assert not ledger.should_skip("n1", "https://example.com/fail", retry_failed=True)


def test_ledger_survives_reopen_and_unfinished_state_resumes(tmp_path):
    ledger = SupplementLedger(tmp_path)
    ledger.set_state("n1", "https://example.com/a", "running")
    reopened = SupplementLedger(tmp_path)
    assert reopened.get("n1", "https://example.com/a")["state"] == "running"
    assert not reopened.should_skip("n1", "https://example.com/a")
