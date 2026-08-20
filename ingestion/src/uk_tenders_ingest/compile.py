"""Compile releases → current state, project the curated row, and diff changes.

Merge (ADR-0001/0005): releases are sorted by date ascending with a deterministic
secondary sort on release id (the canonical ocds-merge tie-break is input-order, which
is non-deterministic across re-crawls — we impose determinism). The default merge is a
self-contained, offline fold (later non-null wins; null deletes; id-keyed arrays merge
per element; other lists whole-list-merge). Set USE_OCDSMERGE=1 to use the canonical
`ocdsmerge` library instead; a conformance test compares the two on fixtures.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from .regime import process_regime, regime_for_release


def _sort_key(release: dict[str, Any]) -> tuple[str, str]:
    return (str(release.get("date") or ""), str(release.get("id") or ""))


def sort_releases(releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(releases, key=_sort_key)


# --------------------------------------------------------------------------- merge

def _merge_into(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, val in overlay.items():
        if val is None:
            base.pop(key, None)
            continue
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _merge_into(base[key], val)
        elif isinstance(val, list) and _is_id_list(val) and _is_id_list(base.get(key)):
            base[key] = _merge_id_lists(base[key], val)
        else:
            base[key] = val
    return base


def _is_id_list(val: Any) -> bool:
    return isinstance(val, list) and all(isinstance(x, dict) and "id" in x for x in val) and len(val) > 0


def _merge_id_lists(base: list[dict], overlay: list[dict]) -> list[dict]:
    out = {str(x["id"]): dict(x) for x in base}
    for x in overlay:
        xid = str(x["id"])
        if xid in out and isinstance(out[xid], dict):
            _merge_into(out[xid], x)
        else:
            out[xid] = dict(x)
    # Stable sort on id so re-crawls produce identical compiled output (idempotency).
    return sorted(out.values(), key=lambda x: str(x.get("id", "")))


def _simple_merge(releases: list[dict[str, Any]]) -> dict[str, Any]:
    compiled: dict[str, Any] = {}
    for rel in releases:
        # ignore release envelope-only keys that shouldn't accumulate
        payload = {k: v for k, v in rel.items() if k not in ("tag",)}
        _merge_into(compiled, payload)
    compiled["tag"] = ["compiled"]
    return compiled


def compile_release(releases: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sort_releases(releases)
    if os.environ.get("USE_OCDSMERGE") == "1":
        try:
            from ocdsmerge import Merger  # type: ignore

            return Merger().create_compiled_release(ordered)
        except Exception:  # noqa: BLE001, S110 — fall back to offline merge
            pass
    return _simple_merge(ordered)


# ----------------------------------------------------------------- field helpers

def _iso(value: Any) -> str | None:
    """Normalise an OCDS date/datetime string to a full ISO timestamp BigQuery accepts.

    OCDS dates may be date-only ('2026-06-19'), Zulu ('...Z'), or offset ('...+01:00').
    BigQuery TIMESTAMP rejects date-only values, so we always emit a time component.
    """
    if not isinstance(value, str) or not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(s[:10]).isoformat()  # date-only → midnight
        except ValueError:
            return None


def _party_name(release: dict[str, Any], role: str) -> tuple[str | None, str | None]:
    # Prefer a direct reference (buyer), else scan parties for the role.
    if role == "buyer":
        b = release.get("buyer")
        b = b if isinstance(b, dict) else {}
        if b.get("name") or b.get("id"):
            return as_text(b.get("name")), as_text(b.get("id"))
    for p in release.get("parties", []) or []:
        if not isinstance(p, dict):
            continue
        if role in (p.get("roles") or []):
            return as_text(p.get("name")), as_text(p.get("id"))
    return None, None


def _cpv_codes(tender: dict[str, Any]) -> list[str]:
    codes: list[str] = []

    def add(cl: Any) -> None:
        if isinstance(cl, dict) and str(cl.get("scheme", "")).upper() == "CPV" and cl.get("id"):
            codes.append(str(cl["id"]))

    add(tender.get("classification"))
    for item in tender.get("items", []) or []:
        add(item.get("classification"))
        for ac in item.get("additionalClassifications", []) or []:
            add(ac)
    # de-dup, preserve order
    seen: set[str] = set()
    return [c for c in codes if not (c in seen or seen.add(c))]


def _region(tender: dict[str, Any]) -> str | None:
    for item in tender.get("items", []) or []:
        for addr in item.get("deliveryAddresses", []) or []:
            if addr.get("region"):
                return addr["region"]
        dl = item.get("deliveryLocation") or {}
        if dl.get("description"):
            return dl["description"]
    return None


def _stage(tags: set[str]) -> str:
    if tags & {"award", "contract", "implementation"}:
        return "award"
    if "tender" in tags:
        return "tender"
    return "planning"


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_text(value: Any) -> str | None:
    """Coerce a value destined for a STRING column to text.

    Malformed OCDS sometimes provides an array (or object) where a scalar string is
    expected (e.g. buyer.name as a list) — which BigQuery rejects for a non-repeated
    column. Join string-ish list elements; drop objects.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [str(x) for x in value if isinstance(x, (str, int, float)) and str(x).strip()]
        return "; ".join(parts) or None
    return None


# --------------------------------------------------------------- projection

def project_process(
    ocid: str, source: str, releases: list[dict[str, Any]], adapter
) -> dict[str, Any]:
    """Build a `compiled_process` row from a process's releases (verbatim, no redaction)."""
    ordered = sort_releases(releases)
    compiled = compile_release(ordered)
    tender = compiled.get("tender") or {}

    all_tags: set[str] = set()
    regimes: list[str] = []
    for rel in ordered:
        all_tags.update(rel.get("tag") or [])
        regimes.append(
            regime_for_release(adapter.notice_type(rel), _iso(rel.get("date")), source)
        )

    buyer_name, buyer_id = _party_name(compiled, "buyer")
    value = tender.get("value") or {}
    cpv = _cpv_codes(tender)

    awards_out = []
    awarded_by_ccy: dict[str, float] = {}
    for aw in compiled.get("awards", []) or []:
        av = aw.get("value") or {}
        amount = _num(av.get("amount"))
        ccy = av.get("currency")
        suppliers = aw.get("suppliers") or []
        sup = suppliers[0] if suppliers else {}
        awards_out.append(
            {
                "award_id": aw.get("id"),
                "status": aw.get("status"),
                "amount": amount,
                "currency": ccy,
                "supplier_name": as_text(sup.get("name")),
                "supplier_id": as_text(sup.get("id")),
                "date": _iso(aw.get("date")),
            }
        )
        if aw.get("status") == "active" and amount is not None and ccy:
            awarded_by_ccy[ccy] = awarded_by_ccy.get(ccy, 0.0) + amount

    awarded_currency = max(awarded_by_ccy, key=awarded_by_ccy.get) if awarded_by_ccy else None
    awarded_amount = awarded_by_ccy.get(awarded_currency) if awarded_currency else None

    parties_out = [
        {"id": as_text(p.get("id")), "name": as_text(p.get("name")), "roles": p.get("roles") or []}
        for p in (compiled.get("parties") or [])
        if isinstance(p, dict)
    ]

    dates = [d for d in (_iso(r.get("date")) for r in ordered) if d]
    latest = ordered[-1] if ordered else {}

    return {
        "ocid": ocid,
        "source": source,
        "process_group_id": None,  # set by the matcher
        "title": as_text(tender.get("title")),
        "description": as_text(tender.get("description")),
        "buyer_name": buyer_name,
        "buyer_id": buyer_id,
        "status": as_text(tender.get("status")),
        "stage": _stage(all_tags),
        "regime": process_regime(regimes),
        "main_category": as_text(tender.get("mainProcurementCategory")),
        "value_amount": _num(value.get("amount")),
        "value_currency": value.get("currency"),
        "awarded_amount": awarded_amount,
        "awarded_currency": awarded_currency,
        "cpv_codes": cpv,
        "cpv_division": cpv[0][:2] if cpv else None,
        "region": as_text(_region(tender)),
        "published_date": min(dates) if dates else None,
        "tender_end_date": _iso((tender.get("tenderPeriod") or {}).get("endDate")),
        "last_updated": max(dates) if dates else None,
        "official_url": adapter.notice_url(latest),
        "awards": awards_out,
        "parties": parties_out,
        "compiled_json": compiled,
    }


# ------------------------------------------------------------------ change diff

def diff_process(
    ocid: str, source: str, releases: list[dict[str, Any]], adapter
) -> list[dict[str, Any]]:
    """Field-level changes across date-ordered releases (PRD §9, provisional taxonomy)."""
    ordered = sort_releases(releases)
    changes: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for rel in ordered:
        tender = rel.get("tender") or {}
        if prev is not None:
            ptender = prev.get("tender") or {}
            url = adapter.notice_url(rel)
            changed_at = _iso(rel.get("date"))
            # deadline change
            new_end = (tender.get("tenderPeriod") or {}).get("endDate")
            old_end = (ptender.get("tenderPeriod") or {}).get("endDate")
            if new_end and new_end != old_end:
                changes.append(
                    _change(ocid, source, "deadline_changed", "tender.tenderPeriod.endDate",
                            old_end, new_end, rel.get("id"), changed_at, url)
                )
            # value change
            new_val = (tender.get("value") or {}).get("amount")
            old_val = (ptender.get("value") or {}).get("amount")
            if new_val is not None and new_val != old_val:
                changes.append(
                    _change(ocid, source, "value_changed", "tender.value.amount",
                            str(old_val), str(new_val), rel.get("id"), changed_at, url)
                )
            # status → cancelled/withdrawn
            new_status = tender.get("status")
            old_status = ptender.get("status")
            if new_status in ("cancelled", "withdrawn") and new_status != old_status:
                changes.append(
                    _change(ocid, source, "cancelled", "tender.status",
                            old_status, new_status, rel.get("id"), changed_at, url)
                )
            # new award
            if "award" in (rel.get("tag") or []) and "award" not in (prev.get("tag") or []):
                changes.append(
                    _change(ocid, source, "new_award", "awards", None, "award_published",
                            rel.get("id"), changed_at, url)
                )
        prev = rel
    return changes


def _change(ocid, source, klass, path, old, new, release_id, changed_at, url) -> dict[str, Any]:
    return {
        "ocid": ocid,
        "source": source,
        "change_class": klass,
        "field_path": path,
        "old_value": old,
        "new_value": new,
        "release_id": release_id,
        "changed_at": changed_at,
        "official_url": url,
    }
