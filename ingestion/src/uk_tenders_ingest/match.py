"""Cross-source identity resolution → process_group (ADR-0002).

OCID is publisher-scoped, so the same real procurement on FTS + Contracts Finder (+ devolved
portals) carries different OCIDs. This builds a derived `process_group_id` linking likely-same
processes via blocking + fuzzy scoring, and stamps it back onto compiled_process.

Run after ingestion:  python -m uk_tenders_ingest.match
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections import defaultdict

from google.cloud import bigquery

from .config import Settings

_LEGAL = re.compile(
    r"\b(ltd|limited|plc|llp|llc|inc|the|uk|services|service|group|holdings|cic|council|nhs|trust)\b"
)
_NONWORD = re.compile(r"[^a-z0-9 ]")
_WS = re.compile(r"\s+")


def norm(name: str | None) -> str:
    if not name:
        return ""
    s = _NONWORD.sub(" ", name.lower())
    s = _LEGAL.sub(" ", s)
    return _WS.sub(" ", s).strip()


def tokens(text: str | None) -> set[str]:
    return {t for t in _NONWORD.sub(" ", (text or "").lower()).split() if len(t) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _block_key(r: dict) -> tuple:
    month = (str(r.get("published_date") or "")[:7])
    return (norm(r.get("buyer_name")), r.get("cpv_division") or "", month)


def _same_process(a: dict, b: dict) -> tuple[bool, float, str]:
    """Decide whether two processes (already in the same block) are the same procurement."""
    title_sim = jaccard(tokens(a.get("title")), tokens(b.get("title")))
    va, vb = a.get("value_amount"), b.get("value_amount")
    value_match = (
        va is not None and vb is not None and a.get("value_currency") == b.get("value_currency")
        and abs(float(va) - float(vb)) <= max(1.0, 0.01 * float(va))
    )
    if title_sim >= 0.7 and value_match:
        return True, 0.95, "fuzzy:title+value"
    if title_sim >= 0.85:
        return True, 0.85, "fuzzy:title"
    if value_match and title_sim >= 0.4:
        return True, 0.8, "fuzzy:value+title"
    return False, 0.0, ""


def _group_id(ocids: list[str]) -> str:
    h = hashlib.sha256("|".join(sorted(ocids)).encode()).hexdigest()[:16]
    return f"pg-{h}"


def run(project: str, raw_dataset: str, public_dataset: str, location: str) -> dict:
    client = bigquery.Client(project=project, location=location)
    rows = [
        dict(r)
        for r in client.query(
            f"""SELECT ocid, source, buyer_name, title, value_amount, value_currency,
                       cpv_division, CAST(published_date AS STRING) AS published_date
                FROM `{project}.{public_dataset}.compiled_process`"""
        ).result()
    ]

    # Block, then union likely-same processes within each block (cross-source pairs first).
    blocks: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        blocks[_block_key(r)].append(r)

    parent: dict[str, str] = {r["ocid"]: r["ocid"] for r in rows}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    edges: dict[tuple[str, str], tuple[float, str]] = {}
    for members in blocks.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a["source"] == b["source"]:
                    continue  # within-source already compiled by OCID; we want cross-source links
                ok, conf, method = _same_process(a, b)
                if ok:
                    union(a["ocid"], b["ocid"])
                    edges[(a["ocid"], b["ocid"])] = (conf, method)

    clusters: dict[str, list[str]] = defaultdict(list)
    for ocid in parent:
        clusters[find(ocid)].append(ocid)

    src_of = {r["ocid"]: r["source"] for r in rows}
    pg_rows = []
    multi = 0
    for members in clusters.values():
        gid = _group_id(members)
        if len(members) > 1:
            multi += 1
        for ocid in members:
            conf = 1.0 if len(members) == 1 else 0.9
            method = "singleton" if len(members) == 1 else "fuzzy"
            pg_rows.append(
                {"process_group_id": gid, "ocid": ocid, "source": src_of[ocid], "confidence": conf, "method": method}
            )

    # Replace process_group and stamp process_group_id back onto compiled_process.
    pg_tbl = f"{project}.{public_dataset}.process_group"
    client.query(f"TRUNCATE TABLE `{pg_tbl}`").result()
    if pg_rows:
        client.load_table_from_json(
            pg_rows, pg_tbl,
            job_config=bigquery.LoadJobConfig(schema=client.get_table(pg_tbl).schema, write_disposition="WRITE_APPEND"),
        ).result()
    client.query(
        f"""UPDATE `{project}.{public_dataset}.compiled_process` c
            SET process_group_id = g.process_group_id
            FROM `{pg_tbl}` g WHERE c.ocid = g.ocid"""
    ).result()

    return {"processes": len(rows), "groups": len(clusters), "multi_source_or_dup_groups": multi}


def main(argv: list[str] | None = None) -> int:
    s = Settings.from_env()
    summary = run(s.project, s.raw_dataset, s.public_dataset, s.bq_location)
    import json

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
