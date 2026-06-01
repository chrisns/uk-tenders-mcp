# OCDS personal data — where it appears (handling note)

The index re-publishes UK procurement OCDS **verbatim** ([ADR-0006](adr/0006-serve-source-data-verbatim.md)): personal data is **not** redacted — it is reproduced exactly as the source authorities publish it as open data (OGL v3.0). This note documents *where* personal data appears, for transparency and to support takedown handling (PRD §10.1). It is **not** a redaction policy; nothing listed here is stripped.

## Where personal data appears in the record

| OCDS path (anywhere in the tree) | Kind |
|---|---|
| `*.contactPoint.name` | Named individual |
| `*.contactPoint.email` | Personal/role email |
| `*.contactPoint.telephone`, `*.contactPoint.faxNumber` | Phone / fax |
| `parties[].name`, `buyer.name`, `awards[].suppliers[].name` | Organisation names — but a sole-trader/partnership name may be personal |
| `tender.title`, `tender.description`, other free text | May contain personal data a buyer typed in |

All of the above is served as published. The **official notice URL** on every record links back to the source authority's authoritative copy.

## Controls

- **Provenance:** official URL + OGL v3.0 attribution on every record; the index is a faithful mirror, **not** the authority of record.
- **Takedown:** a published contact + takedown/erasure route is the residual control; requests are also directed to the source authority as data controller.
- **Isolation (not redaction):** the public read-only dataset is a least-privilege boundary ([ADR-0001](adr/0001-bigquery-index-with-gcs-raw-and-star-schema.md), [ADR-0003](adr/0003-public-mcp-readonly-sql-secured-by-iam.md)) — it limits what the public API can reach, independent of content.
