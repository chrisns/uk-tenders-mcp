# Examples

Live endpoint: `https://tenders.run.cns.me/mcp`

## Add to Claude Code

```bash
claude mcp add --transport http uk-tenders https://tenders.run.cns.me/mcp
```

## Questions to ask your assistant

- "Break down UK tenders by CPV division — which sectors dominate?"
- "Which buyers have published the most tenders this month? Link the official notices."
- "Find live (`stage=tender`) construction tenders over £500k and summarise them."
- "What's the awarded-value leaderboard of suppliers, and how concentrated is the top 10?"
- "What changed on `ocds-h6vhtk-06a9b3` recently?"
- "Is the index fresh? When did it last update?"

## Raw MCP (Node, official SDK)

```bash
node api/test/smoke.mjs https://tenders.run.cns.me/mcp
```

## Health

```bash
curl -s https://tenders.run.cns.me/health
# {"status":"ok","service":"uk-tenders-mcp","version":"0.1.0"}
```

## Example analytics via `query_sql`

```sql
-- awarded value by CPV division (GBP), top 10
SELECT cpv_division, ROUND(SUM(awarded_amount)) AS gbp, COUNT(*) AS n
FROM uk_tenders_public.compiled_process
WHERE awarded_currency = 'GBP' AND cpv_division IS NOT NULL
GROUP BY cpv_division ORDER BY gbp DESC LIMIT 10;
```

Values are **awarded contract value, not actual spend**. Every record links to its official
Find a Tender notice; always verify critical details there.
