#!/usr/bin/env npx tsx
/**
 * fetch_fraud.ts
 *
 * Fetches IS_FRAUD and IS_BAD from PROD.ANALYTICS.RISK_PAYMENTS for all
 * payment IDs in sheet_data.csv. Outputs snowflake_fraud.csv.
 *
 * Usage:
 *   npx tsx scripts/fetch_fraud.ts [sheet_data.csv] [snowflake_fraud.csv]
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
import { parse } from 'csv-parse/sync';
import { stringify } from 'csv-stringify/sync';

const sheetCsv = process.argv[2] ?? '/tmp/risk_intel/sheet_data.csv';
const outputCsv = process.argv[3] ?? '/tmp/risk_intel/snowflake_fraud.csv';

const PAT = process.env.SNOWFLAKE_PAT;
const HOST = process.env.SNOWFLAKE_HOST ?? 'mya82408.us-east-1.snowflakecomputing.com';
const WAREHOUSE = process.env.SNOWFLAKE_WAREHOUSE ?? 'COMPUTE_WH';
const ROLE = process.env.SNOWFLAKE_ROLE ?? 'SNOWFLAKE_MCP';

if (!PAT) { console.error('Missing env var: SNOWFLAKE_PAT'); process.exit(1); }

const rows = parse(fs.readFileSync(sheetCsv, 'utf8'), { columns: true }) as Record<string, string>[];
const paymentIds = [...new Set(rows.map(r => r.payment_id).filter(Boolean))];
console.log(`${paymentIds.length} unique payment IDs to fetch`);

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
    body: JSON.stringify({ statement: sql, timeout: 120, warehouse: WAREHOUSE, role: ROLE }),
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

  const data: string[][] = [...(body.data ?? [])];
  const partitions: any[] = body.resultSetMetaData.partitionInfo ?? [];
  const handle: string = body.statementHandle;

  for (let i = 1; i < partitions.length; i++) {
    const r = await fetch(`https://${HOST}/api/v2/statements/${handle}?partition=${i}`, { headers: AUTH_HEADERS });
    const b: any = await r.json();
    if (!r.ok) throw new Error(`Partition ${i}: ${b.message}`);
    data.push(...(b.data ?? []));
  }

  return { colIndex, data };
}

async function main() {
  // RISK_PAYMENTS.payment_id is NUMBER — pass as unquoted numeric literals
  const inList = paymentIds.join(',');
  const sql = `
    SELECT rp.payment_id::VARCHAR AS payment_id, rp.is_fraud, rp.is_bad
    FROM PROD.ANALYTICS.RISK_PAYMENTS rp
    WHERE rp.payment_id IN (${inList})
  `;

  const { colIndex, data } = await runStatement(sql);
  console.log(`Snowflake returned ${data.length} rows`);

  const found = new Map(data.map(row => [row[colIndex('PAYMENT_ID')], row]));

  const output = paymentIds.map(id => {
    const row = found.get(id);
    return {
      payment_id: id,
      is_fraud: row ? row[colIndex('IS_FRAUD')] : 'false',
      is_bad:   row ? row[colIndex('IS_BAD')]   : 'false',
    };
  });

  const missing = paymentIds.length - found.size;
  if (missing > 0) console.log(`${missing} IDs not in RISK_PAYMENTS → treated as is_fraud=false`);

  fs.writeFileSync(outputCsv, stringify(output, { header: true }));
  console.log(`Written to ${outputCsv}`);
}

main().catch(err => { console.error(err.message); process.exit(1); });
