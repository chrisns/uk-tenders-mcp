from uk_tenders_ingest.canonical import content_hash, redact


def test_content_hash_is_stable_and_ignores_published_date():
    a = {"ocid": "x", "id": "1", "tag": ["tender"], "publishedDate": "2026-01-01T00:00:00Z"}
    b = {"ocid": "x", "id": "1", "tag": ["tender"], "publishedDate": "2026-02-02T00:00:00Z"}
    # publishedDate is volatile and stripped before hashing → equal hashes
    assert content_hash(a) == content_hash(b)


def test_content_hash_changes_on_real_change():
    a = {"ocid": "x", "id": "1", "tender": {"title": "A"}}
    b = {"ocid": "x", "id": "1", "tender": {"title": "B"}}
    assert content_hash(a) != content_hash(b)


def test_redact_strips_contact_personal_fields_but_keeps_url():
    rel = {
        "parties": [
            {
                "id": "GB-1",
                "name": "Acme",
                "contactPoint": {
                    "name": "Jane Doe",
                    "email": "jane@example.com",
                    "telephone": "0123",
                    "faxNumber": "0124",
                    "url": "https://acme.example",
                },
            }
        ]
    }
    out = redact(rel)
    cp = out["parties"][0]["contactPoint"]
    assert "name" not in cp and "email" not in cp and "telephone" not in cp and "faxNumber" not in cp
    assert cp["url"] == "https://acme.example"
    # original is untouched (deep copy)
    assert rel["parties"][0]["contactPoint"]["email"] == "jane@example.com"


def test_redact_strips_contacts_at_every_depth():
    # contactPoint personal data nested in awards[].suppliers[] and procuringEntity must all go.
    import json

    rel = {
        "parties": [{"id": "S1", "name": "Sup Co", "contactPoint": {"name": "Alice", "email": "a@x.test"}}],
        "tender": {"procuringEntity": {"id": "B1", "contactPoint": {"name": "Carol", "email": "c@x.test"}}},
        "awards": [
            {
                "id": "a1",
                "suppliers": [
                    {"id": "S1", "name": "Sup Co", "contactPoint": {"name": "Bob", "email": "b@x.test", "telephone": "999"}}
                ],
            }
        ],
    }
    blob = json.dumps(redact(rel))
    for personal in ("a@x.test", "b@x.test", "c@x.test", "Alice", "Bob", "Carol", "999"):
        assert personal not in blob, f"leaked: {personal}"
    # company names (not personal contact fields) are retained
    assert "Sup Co" in blob
