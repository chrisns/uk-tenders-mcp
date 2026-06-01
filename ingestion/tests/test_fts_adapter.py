"""FTS adapter pagination: follow links.next; stop when the `links` key is absent."""

from uk_tenders_ingest.adapters.fts import FTSAdapter


class FakeResp:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.text = ""
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, headers=None, timeout=None, verify=True):
        self.calls.append(url)
        return FakeResp(self.mapping[url])


def test_pagination_follows_links_next_then_stops_when_absent():
    base = "https://www.find-tender.service.gov.uk/api/1.0"
    page1_url = f"{base}/ocdsReleasePackages?limit=100&updatedFrom=2026-05-01T00:00:00&updatedTo=2026-05-02T00:00:00"
    page2_url = f"{base}/ocdsReleasePackages?cursor=ABC"
    mapping = {
        page1_url: {
            "releases": [{"ocid": "o1", "id": "1-2026"}, {"ocid": "o2", "id": "2-2026"}],
            "links": {"next": page2_url},
        },
        # page 2 has NO `links` key → termination signal
        page2_url: {"releases": [{"ocid": "o3", "id": "3-2026"}]},
    }
    sess = FakeSession(mapping)
    adapter = FTSAdapter(session=sess)
    ocids = [r["ocid"] for r in adapter.iter_releases("2026-05-01T00:00:00", "2026-05-02T00:00:00")]
    assert ocids == ["o1", "o2", "o3"]
    assert sess.calls == [page1_url, page2_url]


def test_notice_url_uses_notice_id():
    adapter = FTSAdapter()
    url = adapter.notice_url({"id": "051129-2026", "ocid": "ocds-h6vhtk-06a9b1"})
    assert url == "https://www.find-tender.service.gov.uk/Notice/051129-2026"
