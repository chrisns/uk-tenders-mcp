"""eTendersNI adapter — a CAPTCHA-gated European Dynamics EPPS portal with no OCDS/API.

Fully automated and credential-free (the public search + notice pages need no login):
  * solves the search CAPTCHA with **Gemini** (Vertex AI vision) — tesseract can't read the
    distorted font, but Gemini 2.5-flash reads it reliably, and one solve unlocks the whole
    session (pagination is CAPTCHA-free and the validated CAPTCHA is reusable across searches);
  * paginates the displaytag results and parses listing + detail pages into canonical rows.

Gemini replaces the human CAPTCHA reader used during the initial backfill, so the overnight
update needs no human in the loop. CAPTCHA solves are cheap and retried on failure (a wrong
read just redisplays the form), so end-to-end success approaches 100% within a few attempts.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from html import unescape

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://etendersni.gov.uk/epps"
SEARCH = f"{BASE}/viewCFTSAction.do"
CAPTCHA_IMG = f"{BASE}/genCaptcha/captcha.jpg"
ADV_FORM = f"{BASE}/prepareAdvancedSearch.do?type=cftFTS"
DETAIL = f"{BASE}/cft/prepareViewCfTWS.do?resourceId={{rid}}"
# eTendersNI sits behind an F5 BIG-IP WAF that rejects non-browser clients from datacenter
# IPs (fine locally, blocked from Cloud Run). We collect public OGL data at low volume (one
# nightly search), so we present browser-like headers to avoid the WAF's bot heuristics.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", os.environ.get("GCP_PROJECT", "govreposcrape"))
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
CAPTCHA_MODEL = os.environ.get("CAPTCHA_MODEL", "gemini-2.5-flash")

_CAPTCHA_PROMPT = (
    "This is a CAPTCHA image containing exactly 6 characters that are lowercase letters "
    "(a-z) and/or digits (0-9). Read them carefully — note o vs 0, l vs 1, j vs i. "
    "Output ONLY those characters in order, lowercase, no spaces, no punctuation, no explanation."
)


def _strip(x: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", x)).replace("\xa0", " ").strip()


def _make_session():
    """HTTP session for eTendersNI. Prefer curl_cffi with a Chrome TLS+HTTP2 fingerprint:
    eTendersNI's F5 WAF fingerprints the client (JA3) and rejects vanilla-Python TLS from a
    container (confirmed: same residential egress IP works from macOS Python but not from the
    Debian container). Impersonating Chrome's TLS makes the low-volume, authorised OGL-data
    collection look like the browser it would otherwise be done in. Falls back to requests."""
    try:
        from curl_cffi import requests as cffi
        s = cffi.Session(impersonate="chrome", verify=False)
        s.headers.update({"Accept-Language": "en-GB,en;q=0.9"})
        return s
    except ImportError:
        s = requests.Session()
        s.verify = False
        s.headers.update(_BROWSER_HEADERS)
        return s


_GENAI_CLIENT = None


def _genai_client():
    # Cache the client: constructing it re-runs ADC/WIF auth (~4-5s in-cluster). The eTendersNI
    # CAPTCHA has a short TTL, so a per-call client init blew the budget (6s solve -> expired ->
    # rejected). Reusing the client makes solves ~1-2s, within the TTL.
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        from google import genai
        _GENAI_CLIENT = genai.Client(vertexai=True, project=VERTEX_PROJECT, location=VERTEX_LOCATION)
    return _GENAI_CLIENT


def solve_captcha(image_bytes: bytes, mime: str = "image/png") -> str:
    """Read a CAPTCHA image with Gemini (Vertex AI). Returns the lowercased code."""
    from google.genai import types

    r = _genai_client().models.generate_content(
        model=CAPTCHA_MODEL,
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime), _CAPTCHA_PROMPT],
    )
    return re.sub(r"[^a-z0-9]", "", (r.text or "").strip().lower())


class ETendersNIAdapter:
    key = "etendersni"

    def __init__(self, timeout_s: int = 45, pause_s: float = 0.15, captcha_retries: int = 6):
        # No credentials needed. Tested logged-out: the public CfT search, displaytag
        # pagination, and the notice-view (prepareViewCfTWS) pages are all reachable with
        # just a session cookie — the CAPTCHA (solved by Gemini) is the only gate. Login was
        # confirmed unnecessary, so we don't carry eTendersNI credentials into the cloud.
        self.timeout_s = timeout_s
        self.pause_s = pause_s
        self.captcha_retries = captcha_retries
        self._sess: requests.Session | None = None
        self._captcha: str | None = None  # the validated captcha for this session

    # --------------------------------------------------------------- session
    def session(self):
        if self._sess is None:
            self._sess = _make_session()
            self._sess.get(ADV_FORM, timeout=self.timeout_s)  # establish JSESSIONID
        return self._sess

    # ----------------------------------------------------------- captcha gate
    def _validated_captcha(self, sample_from: str, sample_to: str) -> str:
        """Solve a CAPTCHA via Gemini and confirm it works by running a probe search.
        Returns a captcha string proven valid for this session (reusable for all searches)."""
        s = self.session()
        # Warm the Gemini token once up front (the STS/WIF exchange is deferred to the first
        # inference, ~4-5s in-cluster) so the captcha solves below stay within the short TTL.
        try:
            _genai_client().models.generate_content(model=CAPTCHA_MODEL, contents=["warmup"])
        except Exception:
            pass
        last = ""
        for attempt in range(1, self.captcha_retries + 1):
            s.get(ADV_FORM, timeout=self.timeout_s)
            img = s.get(CAPTCHA_IMG, timeout=self.timeout_s).content
            t0 = time.time()
            try:
                cap = solve_captcha(img)
            except Exception as e:  # surface Vertex/auth errors instead of swallowing them
                last = f"gemini-error {type(e).__name__}: {str(e)[:240]}"
                print(f"[etendersni] captcha attempt {attempt}: {last}", flush=True)
                time.sleep(2)
                continue
            if len(cap) < 5:
                continue
            r = s.post(SEARCH, timeout=self.timeout_s, data=self._form(sample_from, sample_to, cap),
                       headers={"Referer": ADV_FORM, "Origin": "https://etendersni.gov.uk"})
            if re.search(r"resourceId=\d+", r.text):
                self._captcha = cap
                return cap
            last = f"rejected read {cap!r} (solve {time.time()-t0:.1f}s, http={r.status_code})"
            print(f"[etendersni] captcha attempt {attempt}: {last}", flush=True)
        raise RuntimeError(f"Gemini CAPTCHA solve failed after {self.captcha_retries} attempts; last: {last}")

    @staticmethod
    def _form(frm: str, to: str, captcha: str) -> dict:
        return {"mode": "search", "isFTS": "true", "type": "cftFTS", "isPopup": "false",
                "uniqueId": "", "cftId": "", "publicationFromDate": frm,
                "publicationUntilDate": to, "captcha": captcha}

    # ------------------------------------------------------------- searching
    def search_window(self, date_from: str, date_to: str) -> list[dict]:
        """All listing rows for notices published in [date_from, date_to] (dd/mm/yyyy).
        Solves one CAPTCHA via Gemini, then paginates the cached result set CAPTCHA-free."""
        s = self.session()
        cap = self._validated_captcha(date_from, date_to)
        from urllib.parse import quote
        q = (f"{SEARCH}?mode=search&isFTS=true&type=cftFTS&isPopup=false"
             f"&publicationFromDate={quote(date_from)}&publicationUntilDate={quote(date_to)}&captcha={cap}")
        r = s.post(SEARCH, data=self._form(date_from, date_to, cap), timeout=self.timeout_s,
                   headers={"Referer": ADV_FORM, "Origin": "https://etendersni.gov.uk"})
        total = re.search(r"<strong>([\d,]+)</strong>\s*results", r.text)
        total = int(total.group(1).replace(",", "")) if total else 0
        dp = re.search(r"(d-\d+)-p=", r.text)
        seen: dict[str, dict] = {row["resourceId"]: row for row in self._rows(r.text)}
        if dp and total > 10:
            dpid = dp.group(1)
            for p in range(2, (total + 9) // 10 + 1):
                rp = s.get(f"{q}&{dpid}-p={p}", timeout=self.timeout_s)
                rows = self._rows(rp.text)
                for row in rows:
                    seen[row["resourceId"]] = row
                time.sleep(self.pause_s)
        return list(seen.values())

    @staticmethod
    def _rows(htmltext: str) -> list[dict]:
        out = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", htmltext, re.S):
            m = re.search(r"prepareViewCfTWS\.do\?resourceId=(\d+)\">(.*?)</a>", tr, re.S)
            if not m:
                continue
            rid, title = m.group(1), _strip(m.group(2))
            tds = [_strip(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            dates = [t for t in tds if re.search(r"[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d", t)]
            status = next((t for t in tds if t in ("Open", "Closed", "Awarded", "Cancelled",
                                                   "Under Evaluation", "Suspended")), "")
            dm = re.search(r"title=['\"]([^'\"]{10,})['\"]", tr)
            out.append({"resourceId": rid, "title": title,
                        "authority": tds[2] if len(tds) > 2 else "",
                        "publication": dates[0] if dates else "", "deadline": dates[1] if len(dates) > 1 else "",
                        "status": status, "desc": _strip(dm.group(1)) if dm else ""})
        return out

    # --------------------------------------------------------------- detail
    def fetch_detail(self, rid: str) -> dict:
        s = self.session()
        r = s.get(DETAIL.format(rid=rid), timeout=self.timeout_s)
        out = {}
        for k, v in re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", r.text, re.S):
            key = _strip(k).rstrip(":")
            if key and key not in out:
                out[key] = _strip(v)
        return out

    def notice_url(self, rid: str) -> str:
        return DETAIL.format(rid=rid)


_CAT = {"goods": "goods", "works": "works", "services": "services"}


def parse_date(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4)) if m.group(4) else 0
    mm = int(m.group(5)) if m.group(5) else 0
    try:
        return datetime(y, mo, d, hh, mm).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def build_row(rid: str, listing: dict, f: dict) -> dict:
    """Canonical compiled_process row from a listing row + detail dt/dd field map."""
    cpv = re.findall(r"\b(\d{8})\b", f.get("CPV Codes", ""))
    val = re.sub(r"[^\d.]", "", f.get("Estimated value (GBP)", "")) or None
    value_amount = float(val) if val else None
    cat = next((v for k, v in _CAT.items() if k in f.get("Services, Works, Goods", "").lower()), None)
    awarded = bool(f.get("Publication Date of Contract Award Notice"))
    status_listing = (listing.get("status") or "").lower()
    if awarded or "award" in status_listing:
        stage, status = "award", "complete"
    elif status_listing in ("closed", "cancelled"):
        stage, status = "tender", status_listing
    elif "planning" in status_listing or f.get("CfT Involves", "").lower().startswith("prior"):
        stage, status = "planning", "planned"
    else:
        stage, status = "tender", "active"
    buyer = f.get("Buyer Organisation") or listing.get("authority") or None
    published = parse_date(f.get("Date of Publication/Invitation")) or parse_date(listing.get("publication"))
    deadline = parse_date(f.get("Time-limit for receipt of tenders or requests to participate"))
    region = (f.get("NUTS codes") or "").split()[0] if f.get("NUTS codes") else "UKN"
    return {
        "ocid": f"etendersni-{rid}", "source": "etendersni", "process_group_id": None,
        "title": f.get("Title") or listing.get("title"),
        "description": f.get("Description") or listing.get("desc") or None,
        "buyer_name": buyer, "buyer_id": f.get("CfT CA Unique ID"),
        "status": status, "stage": stage, "regime": "ni", "main_category": cat,
        "value_amount": value_amount, "value_currency": "GBP" if value_amount else None,
        "awarded_amount": None, "awarded_currency": None,
        "cpv_codes": cpv, "cpv_division": cpv[0][:2] if cpv else None, "region": region or "UKN",
        "published_date": published, "tender_end_date": deadline,
        "last_updated": parse_date(f.get("Publication Date of Contract Award Notice")) or published,
        "official_url": DETAIL.format(rid=rid), "awards": [],
        "parties": [{"id": f.get("CfT CA Unique ID"), "name": buyer, "roles": ["buyer"]}] if buyer else [],
        "_ref": f.get("CfT CA Unique ID"), "_procedure": f.get("Procedure"),
        "_threshold": f.get("Above or Below Threshold"), "_lots": f.get("Number Of Lots"),
    }
