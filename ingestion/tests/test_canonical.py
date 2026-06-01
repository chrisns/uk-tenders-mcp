from uk_tenders_ingest.canonical import content_hash


def test_content_hash_is_stable_and_ignores_published_date():
    a = {"ocid": "x", "id": "1", "tag": ["tender"], "publishedDate": "2026-01-01T00:00:00Z"}
    b = {"ocid": "x", "id": "1", "tag": ["tender"], "publishedDate": "2026-02-02T00:00:00Z"}
    # publishedDate is volatile and stripped before hashing → equal hashes
    assert content_hash(a) == content_hash(b)


def test_content_hash_changes_on_real_change():
    a = {"ocid": "x", "id": "1", "tender": {"title": "A"}}
    b = {"ocid": "x", "id": "1", "tender": {"title": "B"}}
    assert content_hash(a) != content_hash(b)
