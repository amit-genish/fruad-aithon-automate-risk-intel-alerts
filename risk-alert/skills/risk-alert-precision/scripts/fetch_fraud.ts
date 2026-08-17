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
 *   SNOWFLAKE_ACCOUNT   — e.g. "melio" (account identifier, no region suffix)
 *   SNOWFLAKE_USER      — your Melio email
 *
 * Optional env vars:
 *   SNOWFLAKE_AUTHENTICATOR  — default: EXTERNALBROWSER (SSO browser pop-up)
 *   SNOWFLAKE_WAREHOUSE      — e.g. "RISK_WH"
 *   SNOWFLAKE_ROLE           — e.g. "RISK_ANALYST"
 */

import snowflake from 'snowflake-sdk';
import fs from 'fs';
import { parse } from 'csv-parse/sync';
import { stringify } from 'csv-stringify/sync';

const sheetCsv = process.argv[2] ?? '/tmp/risk_intel/sheet_data.csv';
const outputCsv = process.argv[3] ?? '/tmp/risk_intel/snowflake_fraud.csv';

for (const v of ['SNOWFLAKE_ACCOUNT', 'SNOWFLAKE_USER']) {
  if (!process.env[v]) { console.error(`Missing env var: ${v}`); process.exit(1); }
}

const rows = parse(fs.readFileSync(sheetCsv, 'utf8'), { columns: true }) as Record<string, string>[];
const paymentIds = [...new Set(rows.map(r => r.payment_id).filter(Boolean))];
console.log(`${paymentIds.length} unique payment IDs to fetch`);

async function connect(): Promise<snowflake.Connection> {
  const conn = snowflake.createConnection({
    account: process.env.SNOWFLAKE_ACCOUNT!,
    username: process.env.SNOWFLAKE_USER!,
    authenticator: (process.env.SNOWFLAKE_AUTHENTICATOR ?? 'EXTERNALBROWSER') as any,
    warehouse: process.env.SNOWFLAKE_WAREHOUSE ?? 'COMPUTE_WH',
    role: process.env.SNOWFLAKE_ROLE ?? 'ENGINEER',
  });
  await conn.connectAsync((err, c) => { if (err) throw err; });
  return conn;
}

function runQuery<T>(conn: snowflake.Connection, sql: string): Promise<T[]> {
  return new Promise((resolve, reject) => {
    conn.execute({
      sqlText: sql,
      complete: (err, _stmt, rows) => (err ? reject(err) : resolve((rows ?? []) as T[])),
    });
  });
}

type FraudRow = { PAYMENT_ID: string; IS_FRAUD: boolean; IS_BAD: boolean };

async function main() {
  const conn = await connect();

  // Single query using a VALUES CTE — no batching needed with a direct connection
  const values = paymentIds.map(id => `('${id.replace(/'/g, "''")}')`).join(',');
  const sql = `
    WITH pids AS (
      SELECT $1::VARCHAR AS payment_id FROM VALUES ${values}
    )
    SELECT rp.payment_id, rp.is_fraud, rp.is_bad
    FROM PROD.ANALYTICS.RISK_PAYMENTS rp
    JOIN pids ON rp.payment_id = pids.payment_id
  `;

  const results = await runQuery<FraudRow>(conn, sql);
  console.log(`Snowflake returned ${results.length} rows`);

  const found = new Map(results.map(r => [String(r.PAYMENT_ID), r]));

  const output = paymentIds.map(id => {
    const r = found.get(id);
    return {
      payment_id: id,
      is_fraud: r ? r.IS_FRAUD : false,
      is_bad: r ? r.IS_BAD : false,
    };
  });

  const missing = paymentIds.length - found.size;
  if (missing > 0) console.log(`${missing} IDs not in RISK_PAYMENTS → treated as is_fraud=false`);

  fs.writeFileSync(outputCsv, stringify(output, { header: true }));
  console.log(`Written to ${outputCsv}`);

  conn.destroy(err => { if (err) console.error(err); });
}

main().catch(err => { console.error(err.message); process.exit(1); });
