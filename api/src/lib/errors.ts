// Uniform MCP tool error envelope (PRD §8.2): { code, message, hint }.
// Returned as isError content so the assistant gets a structured, actionable signal.

export type ToolResult = {
  content: { type: "text"; text: string }[];
  isError?: boolean;
};

export function ok(data: unknown): ToolResult {
  return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
}

export function toolError(code: string, message: string, hint?: string): ToolResult {
  return {
    content: [{ type: "text", text: JSON.stringify({ error: { code, message, hint } }, null, 2) }],
    isError: true,
  };
}

// Map a thrown error (often from BigQuery) to a structured envelope.
export function fromException(err: unknown): ToolResult {
  const msg = err instanceof Error ? err.message : String(err);
  if (/exceed.*maximum.*billed|bytesBilled|would be billed|bytes billed/i.test(msg)) {
    return toolError(
      "QUERY_TOO_LARGE",
      "Query would scan more than the allowed byte cap.",
      "Add filters (date range, source, buyer) or aggregate to reduce scanned bytes.",
    );
  }
  if (/timeout|deadline/i.test(msg)) {
    return toolError("TIMEOUT", "Query exceeded the time limit.", "Narrow the query and retry.");
  }
  if (/Syntax error|Unrecognized name|not found: Table|Name .* not found|Column .* not found/i.test(msg)) {
    // Don't echo raw BigQuery text to anonymous callers — it leaks dataset/table/project names
    // and lets an attacker enumerate schema by probing. Log server-side; return a generic hint.
    console.error("query error:", msg);
    return toolError(
      "INVALID_QUERY",
      "Query is invalid — syntax error or unknown table/column.",
      "Call get_schema to see available tables and columns.",
    );
  }
  console.error("internal error:", msg);
  return toolError("INTERNAL", "An internal error occurred.");
}

// Guard a tool handler so any throw becomes a structured error envelope.
export function guard<A extends unknown[]>(
  fn: (...args: A) => Promise<ToolResult>,
): (...args: A) => Promise<ToolResult> {
  return async (...args: A) => {
    try {
      return await fn(...args);
    } catch (err) {
      return fromException(err);
    }
  };
}
