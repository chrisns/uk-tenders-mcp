from uk_tenders_ingest import compile as comp


class FakeAdapter:
    def notice_url(self, rel):
        return f"https://example/Notice/{rel.get('id')}"

    def notice_type(self, rel):
        return None


R1 = {
    "ocid": "o1",
    "id": "1-2026",
    "date": "2026-05-01T00:00:00Z",
    "tag": ["tender"],
    "buyer": {"name": "Buyer Co", "id": "B1"},
    "parties": [{"id": "B1", "name": "Buyer Co", "roles": ["buyer"]}],
    "tender": {
        "title": "Cloud hosting",
        "description": "hosting services",
        "status": "active",
        "value": {"amount": 100000, "currency": "GBP"},
        "tenderPeriod": {"endDate": "2026-06-01T00:00:00Z"},
        "classification": {"scheme": "CPV", "id": "72000000"},
        "mainProcurementCategory": "services",
    },
}
R2 = {
    "ocid": "o1",
    "id": "2-2026",
    "date": "2026-05-10T00:00:00Z",
    "tag": ["award"],
    "awards": [
        {
            "id": "a1",
            "status": "active",
            "value": {"amount": 90000, "currency": "GBP"},
            "suppliers": [{"id": "S1", "name": "Supplier Ltd"}],
            "date": "2026-05-09T00:00:00Z",
        }
    ],
}


def test_project_process_merges_and_extracts():
    row = comp.project_process("o1", "fts", [R2, R1], FakeAdapter())  # unordered input
    assert row["title"] == "Cloud hosting"
    assert row["buyer_name"] == "Buyer Co"
    assert row["stage"] == "award"
    assert row["status"] == "active"
    assert row["value_amount"] == 100000
    assert row["value_currency"] == "GBP"
    assert row["cpv_codes"] == ["72000000"]
    assert row["cpv_division"] == "72"
    assert row["awarded_amount"] == 90000
    assert row["awarded_currency"] == "GBP"
    assert row["main_category"] == "services"
    assert row["awards"][0]["supplier_name"] == "Supplier Ltd"
    # official_url uses the latest (date-sorted) release id
    assert row["official_url"].endswith("/2-2026")
    # dates normalised to a full ISO timestamp (date-only would break the BigQuery TIMESTAMP load)
    assert row["published_date"].startswith("2026-05-01T00:00:00")


def test_as_text_coerces_arrays_and_objects():
    assert comp.as_text("Acme") == "Acme"
    assert comp.as_text(None) is None
    assert comp.as_text(123) == "123"
    assert comp.as_text(["Buyer A", "Buyer B"]) == "Buyer A; Buyer B"  # malformed array → joined
    assert comp.as_text([{"x": 1}]) is None  # objects dropped


def test_project_process_handles_array_buyer_name():
    # regression: a release with buyer.name as an array must not break the STRING projection
    rel = {"ocid": "o9", "id": "9-2026", "date": "2026-01-01T00:00:00Z", "tag": ["tender"],
           "buyer": {"name": ["Council X", "Council Y"], "id": "B9"},
           "tender": {"title": "Svc", "status": "active"}}
    row = comp.project_process("o9", "fts", [rel], FakeAdapter())
    assert row["buyer_name"] == "Council X; Council Y"


def test_iso_normalises_date_only_zulu_and_offset():
    assert comp._iso("2026-06-19").startswith("2026-06-19T00:00:00")  # date-only → midnight
    assert comp._iso("2026-06-19T10:00:00Z").startswith("2026-06-19T10:00:00")
    assert comp._iso("2026-06-19T10:00:00+01:00").startswith("2026-06-19T10:00:00")
    assert comp._iso(None) is None
    assert comp._iso("not-a-date") is None


def test_diff_detects_new_award():
    changes = comp.diff_process("o1", "fts", [R1, R2], FakeAdapter())
    classes = {c["change_class"] for c in changes}
    assert "new_award" in classes
