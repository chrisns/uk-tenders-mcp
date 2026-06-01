from uk_tenders_ingest.regime import process_regime, regime_for_release


def test_uk_notice_is_pca2023():
    assert regime_for_release("UK4", "2025-03-01", "fts") == "pca2023"


def test_legacy_form_is_legacy():
    assert regime_for_release("F02", "2024-01-01", "fts") == "legacy"


def test_scotland_source_is_scotland():
    assert regime_for_release("UK4", "2025-03-01", "pcs") == "scotland"


def test_date_fallback_before_and_after_cutover():
    assert regime_for_release(None, "2025-02-24T10:00:00Z", "fts") == "pca2023"
    assert regime_for_release(None, "2025-02-23T10:00:00Z", "fts") == "legacy"


def test_process_regime_mixed():
    assert process_regime(["legacy", "pca2023"]) == "mixed"
    assert process_regime(["pca2023", "pca2023"]) == "pca2023"
    assert process_regime(["scotland"]) == "scotland"
