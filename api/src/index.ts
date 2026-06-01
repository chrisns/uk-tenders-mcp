// HTTP entrypoint: Streamable-HTTP MCP server (PRD §11).
// Stateful sessions kept in-memory; Cloud Run runs a single instance (+ session
// affinity) so a session never crosses instances. Public + unauthenticated (ADR-0003).

import express from "express";
import rateLimit from "express-rate-limit";
import { randomUUID } from "node:crypto";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { buildServer } from "./server.js";
import { config } from "./lib/config.js";

const app = express();
app.set("trust proxy", 1); // Cloud Run is behind a proxy; use X-Forwarded-For for per-IP limiting
// Small body cap: tool calls are tiny; this also bounds the size of any `sql` payload.
app.use(express.json({ limit: "256kb" }));

// Per-IP rate limit on the public, unauthenticated MCP endpoint (abuse / cost-DoS guard).
const mcpLimiter = rateLimit({
  windowMs: 60_000,
  max: Number(process.env.RATE_LIMIT_PER_MIN || 60),
  standardHeaders: true,
  legacyHeaders: false,
  message: { jsonrpc: "2.0", error: { code: -32000, message: "Rate limit exceeded; slow down." }, id: null },
});

app.get("/health", (_req, res) => {
  res.json({ status: "ok", service: "uk-tenders-mcp", version: "0.1.0" });
});

const transports: Record<string, StreamableHTTPServerTransport> = {};

app.use("/mcp", mcpLimiter);

app.post("/mcp", async (req, res) => {
  try {
    const sid = req.headers["mcp-session-id"] as string | undefined;
    let transport = sid ? transports[sid] : undefined;

    if (!transport && isInitializeRequest(req.body)) {
      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (id) => {
          transports[id] = transport!;
        },
      });
      transport.onclose = () => {
        if (transport!.sessionId) delete transports[transport!.sessionId];
      };
      const server = buildServer();
      await server.connect(transport);
    } else if (!transport) {
      res.status(400).json({
        jsonrpc: "2.0",
        error: { code: -32000, message: "No valid session; send an initialize request first." },
        id: null,
      });
      return;
    }
    await transport.handleRequest(req, res, req.body);
  } catch (err) {
    console.error("mcp POST error", err);
    if (!res.headersSent) {
      res.status(500).json({ jsonrpc: "2.0", error: { code: -32603, message: "Internal error" }, id: null });
    }
  }
});

// GET (SSE stream) and DELETE (session teardown) for the streamable transport.
const sessionRequest = async (req: express.Request, res: express.Response) => {
  const sid = req.headers["mcp-session-id"] as string | undefined;
  const transport = sid ? transports[sid] : undefined;
  if (!transport) {
    res.status(400).send("Invalid or missing session id");
    return;
  }
  await transport.handleRequest(req, res);
};
app.get("/mcp", sessionRequest);
app.delete("/mcp", sessionRequest);

app.listen(config.port, () => {
  console.log(
    `uk-tenders-mcp MCP API on :${config.port} (project=${config.project}, dataset=${config.publicDataset})`,
  );
});
