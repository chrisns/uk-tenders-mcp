// End-to-end smoke test using the real MCP client against a running server.
//   node test/smoke.mjs [http://localhost:8080/mcp]
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const url = process.argv[2] || "http://localhost:8080/mcp";
const client = new Client({ name: "uk-tenders-smoke", version: "0.1.0" });
await client.connect(new StreamableHTTPClientTransport(new URL(url)));

const { tools } = await client.listTools();
console.log("TOOLS:", tools.map((t) => t.name).join(", "));

async function call(name, args) {
  const r = await client.callTool({ name, arguments: args });
  const text = r.content?.[0]?.text ?? "";
  console.log(`\n## ${name}(${JSON.stringify(args)})${r.isError ? " [isError]" : ""}\n` + text.slice(0, 700));
  return r;
}

await call("get_status", {});
await call("aggregate_tenders", { metric: "count", dimension: "cpv_division", limit: 5 });
await call("search_tenders", { query: "construction", limit: 2, resultMode: "minimal" });
await call("query_sql", { sql: "SELECT regime, COUNT(*) n FROM uk_tenders_public.compiled_process GROUP BY regime" });
await call("query_sql", { sql: "DROP TABLE uk_tenders_public.compiled_process" }); // must be refused
await call("search_tenders", { query: "x", cursor: Buffer.from("9999999999").toString("base64") }); // forged offset → INVALID_CURSOR

await client.close();
console.log("\nSMOKE_OK");
