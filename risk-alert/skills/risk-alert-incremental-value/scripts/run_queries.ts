#!/usr/bin/env npx tsx
/**
 * run_queries.ts
 *
 * Reads modified_queries.json (written by Claude in Step 2), wraps each SQL
 * in query_template.sql, executes against Snowflake, and writes one
 * result_{alert}_{timeframe}.json per entry.
 *
 * Usage:
 *   npx tsx scripts/run_queries.ts [modified_queries.json] [output_dir]
 *
 * modified_queries.json format:
 *   [{ "alert_name": "...", "timeframe": "last_2w"|"mature_90_30", "modified_sql": "..." }]
 *
 * Required env vars:
 *   SNOWFLAKE_PAT   — Snowflake Programmatic Access Token
 *
 * Optional env vars:
 *   SNOWFLAKE_HOST  — default: mya82408.us-east-1.snowflakecomputing.com
 *   SNOWFLAKE_WAREHOUSE
 *   SNOWFLAKE_ROLE
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const modifiedQueriesPath = process.argv[2] ?? '/tmp/risk_intel/modified_queries.json';
const outputDir = process.argv[3] ?? '/tmp/risk_intel';
const templatePath = path.resolve(__dirname, '../references/query_template.sql');

const PAT = process.env.SNOWFLAKE_PAT;
const HOST = process.env.SNOWFLAKE_HOST ?? 'mya82408.us-east-1.snowflakecomputing.com';
const WAREHOUSE = process.env.SNOWFLAKE_WAREHOUSE ?? 'COMPUTE_WH';
const ROLE = process.env.SNOWFLAKE_ROLE ?? 'SNOWFLAKE_MCP';

if (!PAT) { console.error('Missing env var: SNOWFLAKE_PAT'); process.exit(1); }

type QueryEntry = { alert_name: string; timeframe: string; modified_sql?: string; full_sql?: string };

const entries: QueryEntry[] = JSON.parse(fs.readFileSync(modifiedQueriesPath, 'utf8'));
const template = fs.readFileSync(templatePath, 'utf8');

console.log(`${entries.length} queries to run`);

const AUTH_HEADERS = {
  'Authorization': `Bearer ${PAT}`,
  'X-Snowflake-Authorization-Token-Type': 'PROGRAMMATIC_ACCESS_TOKEN',
  'Content-Type': 'application/json',
  'Accept': 'application/json',
};

async function pollUntilDone(handle: string): Promise<any> {
  for (;;) {
    await new Promise(r => setTimeout(r, 2000));
    const resp = await fetch(`https://${HOST}/api/v2/statements/${handle}`, { headers: AUTH_HEADERS });
    if (resp.status !== 202) return resp.json();
  }
}

async function runStatement(sql: string): Promise<{ colIndex: (name: string) => number; data: string[][] }> {
  const resp = await fetch(`https://${HOST}/api/v2/statements`, {
    method: 'POST',
    headers: AUTH_HEADERS,
    body: JSON.stringify({ statement: sql, timeout: 300, warehouse: WAREHOUSE, role: ROLE }),
  });

  let body: any = await resp.json();
  if (resp.status === 202) {
    body = await pollUntilDone(body.statementHandle);
  } else if (!resp.ok) {
    throw new Error(body.message ?? `Snowflake HTTP ${resp.status}`);
  }
  if (!body.resultSetMetaData) throw new Error(body.message ?? JSON.stringify(body));

  const rowType: { name: string }[] = body.resultSetMetaData.rowType;
  const colMap = new Map(rowType.map((c, i) => [c.name.toUpperCase(), i]));
  const colIndex = (name: string) => colMap.get(name.toUpperCase()) ?? -1;

  return { colIndex, data: body.data ?? [] };
}

function stripTrailingOrderBy(sql: string): string {
  return sql.replace(/\s+ORDER\s+BY\s+[\s\S]*?(?:;?\s*$)/i, '').trim();
}

function safeName(alertName: string): string {
  return alertName.replace(/[^a-z0-9]/gi, '_').toLowerCase();
}

async function main() {
  let passed = 0;
  let failed = 0;

  for (const entry of entries) {
    const outPath = path.join(outputDir, `result_${safeName(entry.alert_name)}_${entry.timeframe}.json`);

    try {
      let fullSql: string;
      if (entry.full_sql) {
        fullSql = entry.full_sql;
      } else {
        const alertSql = stripTrailingOrderBy(entry.modified_sql ?? '');
        fullSql = template
          .replace('{MODIFIED_ALERT_SQL}', alertSql)
          .replace('{RUN_DATE}', 'CURRENT_TIMESTAMP()');
      }

      const { colIndex, data } = await runStatement(fullSql);
      const row = data[0] ?? [];

      fs.writeFileSync(outPath, JSON.stringify({
        alert_name: entry.alert_name,
        timeframe: entry.timeframe,
        manual_review_load: Number(row[colIndex('MANUAL_REVIEW_LOAD')] ?? 0),
        fraud_count:        Number(row[colIndex('FRAUD_COUNT')]        ?? 0),
        bad_rate_pct:       Number(row[colIndex('BAD_RATE_PCT')]       ?? 0),
        fraud_tpv:          Number(row[colIndex('FRAUD_TPV')]          ?? 0),
      }, null, 2));

      console.log(`✓ ${entry.alert_name} / ${entry.timeframe}`);
      passed++;
    } catch (err: any) {
      console.error(`✗ ${entry.alert_name} / ${entry.timeframe}: ${err.message}`);
      fs.writeFileSync(outPath, JSON.stringify({
        alert_name: entry.alert_name,
        timeframe: entry.timeframe,
        manual_review_load: 0,
        fraud_count: 0,
        bad_rate_pct: 0,
        fraud_tpv: 0,
        error: err.message,
      }, null, 2));
      failed++;
    }
  }

  console.log(`\nDone: ${passed} succeeded, ${failed} failed`);
}

main().catch(err => { console.error(err.message); process.exit(1); });
