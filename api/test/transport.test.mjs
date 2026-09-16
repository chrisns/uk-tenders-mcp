import { test, expect } from "vitest";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { once } from "node:events";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

test("MCP works without a persistent background GET stream", async () => {
  const reservation = createServer();
  reservation.listen(0, "127.0.0.1");
  await once(reservation, "listening");
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const child = spawn(process.execPath, ["--import", "tsx", "src/index.ts"], {
    env: { ...process.env, PORT: String(port), GCP_PROJECT: "test" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", chunk => { output += chunk; });
  child.stderr.on("data", chunk => { output += chunk; });
  const url = new URL(`http://127.0.0.1:${port}/mcp`);
  const client = new Client({ name: "transport-regression", version: "1.0.0" });
  const transport = new StreamableHTTPClientTransport(url);
  try {
    for (let i = 0; !output.includes("MCP API on") && i < 100; i++) {
      if (child.exitCode !== null) throw new Error(output);
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    expect(output).toContain("MCP API on");
    // Use a real initialized session: rejecting only sessionless GETs leaves the billing bug.
    const init = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {
        protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "probe", version: "1" },
      } }),
      signal: AbortSignal.timeout(3000),
    });
    expect(init.status).toBe(200);
    await init.text();
    const sid = init.headers.get("mcp-session-id");
    expect(sid).toBeTruthy();
    const headers = { Accept: "text/event-stream", "mcp-session-id": sid };
    const get = await fetch(url, { headers, signal: AbortSignal.timeout(2000) });
    await get.body?.cancel();
    expect(get.status).toBe(405);
    expect(get.headers.get("allow")).toBe("POST, DELETE");
    const missingSession = await fetch(url, { signal: AbortSignal.timeout(2000) });
    expect(missingSession.status).toBe(405);
    await missingSession.text();
    const deleted = await fetch(url, { method: "DELETE", headers, signal: AbortSignal.timeout(2000) });
    expect(deleted.status).toBe(200);
    await deleted.text();

    // The SDK attempts its own GET after initialization; 405 must not break normal MCP usage.
    await client.connect(transport);
    const tools = (await client.listTools()).tools;
    expect(tools.length).toBeGreaterThan(0);
    // Exercise tools/call without credentials or a billed query: SQL validation rejects this locally.
    const sqlTool = tools.some(tool => tool.name === "research") ? "research" : "query_sql";
    const rejected = await client.callTool({ name: sqlTool, arguments: { sql: "DELETE FROM forbidden" } });
    expect(rejected.isError).toBe(true);
    await client.ping();
    await transport.terminateSession();
  } finally {
    await client.close();
    if (child.exitCode === null) {
      const exited = once(child, "exit");
      child.kill("SIGTERM");
      await exited;
    }
  }
}, 15000);
