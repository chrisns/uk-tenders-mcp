import { describe, it, expect } from "vitest";
import { validateReadOnlySql } from "../src/lib/sqlguard.js";
import { fromException } from "../src/lib/errors.js";
import { shapeProcess, processCols, type ResultMode } from "../src/lib/format.js";

describe("validateReadOnlySql", () => {
  it("accepts SELECT and WITH", () => {
    expect(validateReadOnlySql("SELECT 1").ok).toBe(true);
    expect(validateReadOnlySql("WITH x AS (SELECT 1) SELECT * FROM x").ok).toBe(true);
    expect(validateReadOnlySql("select * from uk_tenders_public.compiled_process").ok).toBe(true);
  });
  it("rejects DDL/DML, multi-statement, and the raw write dataset", () => {
    expect(validateReadOnlySql("DROP TABLE t").ok).toBe(false);
    expect(validateReadOnlySql("DELETE FROM t").ok).toBe(false);
    expect(validateReadOnlySql("SELECT 1; SELECT 2").ok).toBe(false);
    expect(validateReadOnlySql("SELECT * FROM uk_tenders_raw.release_event_log").ok).toBe(false);
  });
});

describe("fromException", () => {
  it("maps byte-cap errors to QUERY_TOO_LARGE", () => {
    const r = fromException(new Error("Query exceeded limit for bytes billed"));
    expect(JSON.parse(r.content[0].text).error.code).toBe("QUERY_TOO_LARGE");
    expect(r.isError).toBe(true);
  });
  it("maps syntax errors to INVALID_QUERY", () => {
    const r = fromException(new Error("Syntax error: Unexpected token"));
    expect(JSON.parse(r.content[0].text).error.code).toBe("INVALID_QUERY");
  });
});

describe("shapeProcess", () => {
  const row = {
    ocid: "ocds-h6vhtk-1",
    source: "fts",
    title: "T",
    description: "D",
    buyer_name: "B",
    status: "active",
    stage: "tender",
    value_amount: { value: "1000" }, // BigQuery NUMERIC wrapper
    value_currency: "GBP",
    published_date: { value: "2026-05-01T00:00:00Z" }, // BigQuery TIMESTAMP wrapper
    official_url: "https://x/Notice/1-2026",
    cpv_codes: ["45000000"],
    awards: [],
    parties: [],
    compiled_json: JSON.stringify({ ocid: "ocds-h6vhtk-1", tag: ["compiled"] }),
  };
  it("minimal omits description and coerces wrapper types", () => {
    const m = shapeProcess(row, "minimal") as Record<string, unknown>;
    expect(m.official_url).toBe("https://x/Notice/1-2026");
    expect(m.value_amount).toBe("1000");
    expect(m.published_date).toBe("2026-05-01T00:00:00Z");
    expect(m.description).toBeUndefined();
  });
  it("standard includes description and cpv", () => {
    const s = shapeProcess(row, "standard") as Record<string, unknown>;
    expect(s.description).toBe("D");
    expect(s.cpv_codes).toEqual(["45000000"]);
  });
  it("full parses compiled_json", () => {
    const f = shapeProcess(row, "full") as Record<string, unknown>;
    expect((f.compiled as Record<string, unknown>).ocid).toBe("ocds-h6vhtk-1");
  });
});

describe("processCols", () => {
  it("minimal never reads the wide columns", () => {
    const cols = processCols("minimal");
    for (const wide of ["compiled_json", "description", "awards", "parties"]) {
      expect(cols).not.toContain(wide);
    }
  });
  it("only full reads compiled_json", () => {
    expect(processCols("standard")).not.toContain("compiled_json");
    expect(processCols("full")).toContain("compiled_json");
  });
  it("selects every column shapeProcess emits, per mode", () => {
    // A fully-populated row: every field the shaper can read.
    const row = {
      ocid: "x", source: "fts", title: "T", description: "D", buyer_name: "B",
      status: "active", stage: "tender", regime: "pca2023", main_category: "services",
      value_amount: "1", value_currency: "GBP", awarded_amount: "1", awarded_currency: "GBP",
      cpv_codes: ["72"], cpv_division: "72", region: "UK", process_group_id: "pg",
      published_date: "2026-01-01", tender_end_date: "2026-02-01", last_updated: "2026-01-02",
      official_url: "https://x/1", awards: [], parties: [], compiled_json: "{}",
    };
    for (const mode of ["minimal", "standard", "full"] as ResultMode[]) {
      const cols = processCols(mode);
      for (const key of Object.keys(shapeProcess(row, mode))) {
        // `compiled` is derived from the compiled_json column.
        expect(cols).toContain(key === "compiled" ? "compiled_json" : key);
      }
    }
  });
});
