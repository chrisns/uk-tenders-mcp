"""Sell2Wales scraper adapter.

WHY scrape: the Sell2Wales OCDS API *and* the bulk-download page are both broken
upstream — every date-range notice query fails with a SQL-level "Error converting data
type nvarchar to float" (confirmed across all months, all output formats: OCDS/TED ×
JSON/XML/CSV/Excel; reported to the Welsh Government). The per-notice public pages and
the date-windowed search use a different query path and work, so we harvest those.

Flow:
  enumerate(date_from, date_to): drive the ASP.NET search with a published-date window +
    "archived contracts" included, paginate the postback results, collect (web_id, title).
  fetch_notice(web_id): GET the public notice page and parse the labelled fields into a
    canonical compiled_process row (source='sell2wales').

Cross-published notices (above-threshold) carry an ocds-h6vhtk OCID on the page and so
dedup against our Find-a-Tender rows via process_group; Wales-only notices carry
ocds-kuma6s and are net-new coverage.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from html import unescape

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://www.sell2wales.gov.wales"
SEARCH = f"{BASE}/Search/Search_MainPage.aspx"
VIEW = f"{BASE}/search/show/search_view.aspx?ID={{id}}"
UA = "uk-tenders-mcp/0.1 (+https://github.com/chrisns/uk-tenders-mcp) OGL public-data index"

_HIDDEN = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "__PREVIOUSPAGE")


def _hidden(htmltext: str) -> dict[str, str]:
    out = {}
    for n in _HIDDEN:
        m = re.search(r'id="' + n + r'"[^>]*value="([^"]*)"', htmltext)
        out[n] = unescape(m.group(1)) if m else ""
    return out


def _strip(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ").strip()


def _dd(htmltext: str, lbl: str) -> str | None:
    m = re.search(r'id="ctl00_MainBody_' + lbl + r'"[^>]*>(.*?)</dd>', htmltext, re.S)
    return _strip(m.group(1)) if m else None


def _section(htmltext: str, heading: str) -> str | None:
    """Body section: <h3>heading</h3> ... up to the next <h3>/<h2>/</section>."""
    m = re.search(
        r"<h3>\s*" + re.escape(heading) + r"\s*</h3>(.*?)(?=<h[23]>|</section>)",
        htmltext, re.S | re.I,
    )
    return m.group(1) if m else None


def _section_any(htmltext: str, *headings: str) -> str | None:
    """First matching section across modern + legacy heading variants."""
    for h in headings:
        sec = _section(htmltext, h)
        if sec is not None:
            return sec
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def _parse_date(s: str | None) -> str | None:
    """'29 May 2026' or '24 June 2026, 23:59PM' -> ISO timestamp."""
    if not s:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?:,?\s*(\d{1,2}):(\d{2}))?", s)
    if not m:
        return None
    day, mon, yr = int(m.group(1)), _MONTHS.get(m.group(2).lower()), int(m.group(3))
    if not mon:
        return None
    hh = int(m.group(4)) if m.group(4) else 0
    mm = int(m.group(5)) if m.group(5) else 0
    pm = "PM" in s.upper()
    if pm and hh < 12:
        hh += 12
    try:
        return datetime(yr, mon, day, min(hh, 23), mm).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _parse_value(sec: str | None) -> tuple[float | None, str | None]:
    """'1772259.00 GBP to 1772259.00GBP' -> (1772259.00, 'GBP'). Takes the upper bound."""
    if not sec:
        return None, None
    txt = _strip(sec)
    nums = re.findall(r"([0-9][0-9,]*\.?\d*)\s*([A-Z]{3})", txt)
    if not nums:
        return None, None
    amt = max(float(n.replace(",", "")) for n, _ in nums)
    cur = nums[-1][1]
    return amt, cur


def _list_items(sec: str | None) -> list[str]:
    if not sec:
        return []
    return [_strip(li) for li in re.findall(r"<li>(.*?)</li>", sec, re.S)]


# Notice-type code -> (stage, status hint). UK4=Contract (tender), UK3=PIN/planning,
# UK5/UK6=award, F02/F05=tender, F03/F06=award, F01/F04=PIN. Awards also detected by
# presence of an award/supplier section.
_AWARD_TYPES = {"UK5", "UK6", "UK7", "F03", "F06", "F13", "F15", "F25", "53", "56"}
_PLANNING_TYPES = {"UK1", "UK3", "F01", "F04", "52", "201"}


class Sell2WalesScrapeAdapter:
    key = "sell2wales"

    def __init__(self, timeout_s: int = 45, max_retries: int = 6, pause_s: float = 0.34):
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.pause_s = pause_s

    # ----------------------------------------------------------------- http
    def _get(self, sess: requests.Session, url: str) -> requests.Response:
        for attempt in range(1, self.max_retries + 1):
            try:
                r = sess.get(url, timeout=self.timeout_s, verify=False)
                if r.status_code == 200:
                    return r
            except requests.RequestException:
                if attempt == self.max_retries:
                    raise
            time.sleep(min(2 ** attempt, 30))
        return r

    def _post(self, sess: requests.Session, url: str, data: dict) -> requests.Response:
        for attempt in range(1, self.max_retries + 1):
            try:
                r = sess.post(url, data=data, timeout=self.timeout_s, verify=False)
                if r.status_code == 200:
                    return r
            except requests.RequestException:
                if attempt == self.max_retries:
                    raise
            time.sleep(min(2 ** attempt, 30))
        return r

    # --------------------------------------------------------- enumeration
    PAGER = "ctl00$MainBody$rpNoticeResultsPager"

    def enumerate(self, date_from: str, date_to: str) -> list[tuple[str, str]]:
        """Return [(web_id, title)] for notices published in [date_from, date_to]
        (dd/mm/yyyy), including archived. Drives the ASP.NET search + postback paging.

        chkArchivedContractsOnly=on is REQUIRED — it's the only mode that returns
        historical/closed notices (verified: without it, past windows return nothing)."""
        sess = requests.Session()
        sess.headers["User-Agent"] = UA
        r = self._get(sess, SEARCH)
        form = _hidden(r.text)
        form.update({
            "ctl00$ctrlCookieBar$btnAcceptCookies": "Accept",
            "ctl00$MainBody$txtKeywords": "",
            "ctl00$MainBody$txtPublishedFrom": date_from,
            "ctl00$MainBody$txtPublishedTo": date_to,
            "ctl00$MainBody$chkArchivedContractsOnly": "on",
            "ctl00$MainBody$btnSearch": "Search",
        })
        r = self._post(sess, SEARCH, form)
        out: dict[str, str] = {}
        for wid, title in self._scrape_listing(r.text):
            out.setdefault(wid, title)
        total = self._total_pages(r.text)
        page = 1
        while page < total:
            page += 1
            form = _hidden(r.text)
            form.update({"__EVENTTARGET": self.PAGER, "__EVENTARGUMENT": str(page)})
            time.sleep(self.pause_s)
            r = self._post(sess, SEARCH, form)
            before = len(out)
            for wid, title in self._scrape_listing(r.text):
                out.setdefault(wid, title)
            if len(out) == before and page < total:
                # defensive: stop if a page yields nothing new (avoids runaway)
                break
        return list(out.items())

    @staticmethod
    def _total_pages(htmltext: str) -> int:
        m = re.search(r"Page\s+\d+\s+of\s+(\d+)", htmltext)
        return int(m.group(1)) if m else 1

    @staticmethod
    def _scrape_listing(htmltext: str) -> list[tuple[str, str]]:
        rows = []
        # Each result links to search_view.aspx?ID=XXX with the title as the link text.
        for m in re.finditer(
            r'search_view\.aspx\?ID=([A-Za-z0-9]+)"[^>]*>(.*?)</a>', htmltext, re.S
        ):
            wid, title = m.group(1), _strip(m.group(2))
            if title:
                rows.append((wid, title))
        return rows

    # --------------------------------------------------------- notice page
    def fetch_notice(self, sess: requests.Session, web_id: str, title: str = "") -> dict | None:
        r = self._get(sess, VIEW.format(id=web_id))
        if "Bad Page parameters" in r.text or r.status_code != 200:
            return None
        return self.parse_notice(web_id, r.text, title)

    def parse_notice(self, web_id: str, htmltext: str, title: str = "") -> dict | None:
        ocid = _dd(htmltext, "lblOCID")
        if not ocid:
            return None
        notice_type = _dd(htmltext, "lblDocumentType")
        published = _parse_date(_dd(htmltext, "lblPublicationDate"))
        deadline = _parse_date(_dd(htmltext, "lblDeadlineDate"))
        buyer = _dd(htmltext, "lblPublishedBy")
        if not buyer:
            # legacy "Site Notice" layout: authority is in the "1.1 Authority Name and Address" section
            asec = _section_any(htmltext, "Authority Name and Address", "Name and Address of the Authority")
            if asec:
                lines = [ln for ln in (_strip(x) for x in re.split(r"<br\s*/?>|</p>|<p>", asec)) if ln]
                buyer = lines[0] if lines else None
        buyer_id = _dd(htmltext, "lblAuthorityId")
        desc = _strip(_section_any(
            htmltext, "Procurement description",
            "Description of the goods or services required",
            "Description of the requirement", "Short description of the contract",
        ) or "") or None
        proc_ref = _strip(_section(htmltext, "Procurement reference") or "") or None
        main_cat = _strip(_section(htmltext, "Main category") or "") or None
        regions = _list_items(_section_any(htmltext, "Delivery regions", "Region(s) of delivery"))
        region = regions[0].split(" - ")[0].strip() if regions else None
        value_amt, value_cur = _parse_value(_section_any(
            htmltext, "Total value (estimated, excluding VAT)", "Total value",
            "Estimated value", "Total quantity or scope of tender",
        ))
        cstart = _parse_date(_strip(_section(htmltext, "Contract start date (estimated)") or ""))
        cend = _parse_date(_strip(_section(htmltext, "Contract end date (estimated)") or ""))
        cpv_sec = _section_any(htmltext, "CPV classifications", "Notice Coding and Classification")
        cpv = list(dict.fromkeys(re.findall(r"\b(\d{8})\b", cpv_sec))) if cpv_sec else []

        # Award / supplier detection
        awards = self._parse_awards(htmltext)
        awarded_amt = max((a["amount"] for a in awards if a.get("amount")), default=None)
        awarded_cur = next((a["currency"] for a in awards if a.get("currency")), None)

        nt = (notice_type or "").upper()
        is_award = bool(awards) or nt in _AWARD_TYPES
        if is_award:
            stage, status = "award", "complete"
        elif nt in _PLANNING_TYPES:
            stage, status = "planning", "planned"
        else:
            stage, status = "tender", "active"

        return {
            "ocid": ocid,
            "source": "sell2wales",
            "process_group_id": None,
            "title": title or proc_ref or (desc[:140] if desc else None),
            "description": desc,
            "buyer_name": buyer,
            "buyer_id": buyer_id,
            "status": status,
            "stage": stage,
            "regime": "wales",
            "main_category": (main_cat.lower() if main_cat else None),
            "value_amount": value_amt,
            "value_currency": value_cur,
            "awarded_amount": awarded_amt,
            "awarded_currency": awarded_cur,
            "cpv_codes": cpv,
            "cpv_division": (cpv[0][:2] if cpv else None),
            "region": region,
            "published_date": published,
            "tender_end_date": deadline or cend,
            "last_updated": published,
            "official_url": VIEW.format(id=web_id),
            "awards": awards,
            "parties": [{"id": buyer_id, "name": buyer, "roles": ["buyer"]}] if buyer else [],
            "_web_id": web_id,
            "_notice_type": notice_type,
            "_proc_ref": proc_ref,
            "_contract_start": cstart,
            "_contract_end": cend,
        }

    @staticmethod
    def _parse_awards(htmltext: str) -> list[dict]:
        """Parse 'Awarded to'/supplier sections if present (award notices)."""
        awards: list[dict] = []
        # Award notices list suppliers under an "Award information"/"Awarded to" area with
        # supplier name + value. Heuristic: find supplier blocks with a value.
        sec = re.search(r"<h2>\s*Award(?:ed| information| details)?[^<]*</h2>(.*)", htmltext, re.S | re.I)
        if not sec:
            return awards
        block = sec.group(1)
        for sm in re.finditer(
            r"<h3>(.*?)</h3>(.*?)(?=<h3>|<h2>|</section>|$)", block, re.S
        ):
            name = _strip(sm.group(1))
            body = sm.group(2)
            amt, cur = _parse_value(body)
            if name and (amt or "supplier" in body.lower()):
                awards.append({
                    "award_id": None, "status": "active",
                    "amount": amt, "currency": cur,
                    "supplier_name": name, "supplier_id": None, "date": None,
                })
        return awards
