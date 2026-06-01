# OCDS personal-data mapping (redaction policy)

Referenced by [ADR-0003](adr/0003-public-mcp-readonly-sql-secured-by-iam.md) and PRD §10.1. This is the
canonical list of which OCDS paths carry personal data and how each is handled at the boundary
between the **raw** dataset (`uk_tenders_raw`, access-controlled, PII-bearing) and the **public**
dataset (`uk_tenders_public`, the only thing the API can read).

Redaction is implemented in `ingestion/src/uk_tenders_ingest/canonical.py::redact()`, which walks the
entire OCDS tree and removes the listed `contactPoint` fields wherever they appear — including nested
copies under `parties[]`, `tender.procuringEntity`, `awards[].suppliers[]`, `buyer`, etc. Proven by
`ingestion/tests/test_canonical.py::test_redact_strips_contacts_at_every_depth`.

## Stripped before reaching the public dataset

| OCDS path (anywhere in the tree) | Reason |
|---|---|
| `*.contactPoint.name` | Named individual |
| `*.contactPoint.email` | Personal/role email |
| `*.contactPoint.telephone` | Phone |
| `*.contactPoint.faxNumber` | Fax |

`*.contactPoint.url` is **retained** (organisational, not personal).

## Retained (organisational / transparency data, not personal under the public-task basis)

| OCDS path | Note |
|---|---|
| `parties[].name`, `buyer.name`, `awards[].suppliers[].name` | Organisation names. **Caveat:** a sole-trader/partnership name may be personal; flagged for the DPIA. |
| `parties[].identifier`, `*.id` | Org identifiers (Companies House, PPON) |
| `parties[].address`, `*.region` | Organisation address / delivery region |
| `tender.title`, `tender.description` | Free text — DPIA must confirm buyers don't paste personal data here; takedown process covers residual cases |

## Controls

- Personal `contactPoint` fields exist **only** in `uk_tenders_raw` (and the GCS raw archive), neither of which the API service account can read.
- `compiled_json`, the `awards`/`parties` STRUCT columns, and all hot scalars in `uk_tenders_public` are built from the **redacted** compiled release.
- Outstanding before production (PRD §10.1): completed DPIA, stated lawful basis (public task), and a published takedown/erasure route.
