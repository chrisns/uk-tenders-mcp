// Light read-only guard for query_sql. The REAL security boundary is the IAM
// read-only service account + maximumBytesBilled (ADR-0003); this just gives the
// assistant a fast, structured rejection for obviously-disallowed input.

export type SqlGuard = { ok: true; sql: string } | { ok: false; code: string; message: string };

export function validateReadOnlySql(input: string): SqlGuard {
  const sql = input.trim().replace(/;\s*$/, "");
  if (!/^(with|select)\b/i.test(sql)) {
    return { ok: false, code: "FORBIDDEN", message: "Only read-only SELECT/WITH queries are allowed." };
  }
  if (/;/.test(sql)) {
    return { ok: false, code: "FORBIDDEN", message: "Only a single statement is allowed." };
  }
  if (/\b(insert|update|delete|merge|create|drop|alter|truncate|grant|revoke)\b/i.test(sql)) {
    return { ok: false, code: "FORBIDDEN", message: "DDL/DML is not allowed." };
  }
  if (/uk_tenders_raw/i.test(sql)) {
    return { ok: false, code: "FORBIDDEN", message: "The raw write dataset is not queryable; use uk_tenders_public." };
  }
  return { ok: true, sql };
}
