#!/usr/bin/env python3
"""
calculate_precision.py
=======================
Merges sheet data with Snowflake is_fraud results, applies exclusions,
and calculates precision per (alert × time_window).

Fraud tagging (worst case):
  is_fraud = snowflake_is_fraud  OR  risk_status == 'Bad Payment'

Exclusions applied BEFORE counting:
  - Payments not yet reviewed (review_result is null/empty)
  - Payments with review_result == 'No' (legacy auto-added rows)

Precision = fraud_payments / total_reviewed_payments  (per alert, per window)

Usage:
    python calculate_precision.py \\
        --sheet   /tmp/risk_intel/sheet_data.csv \\
        --sf-fraud /tmp/risk_intel/snowflake_fraud.csv \\
        --output  /tmp/risk_intel/precision.csv

snowflake_fraud.csv must have columns: payment_id, is_fraud  (boolean/0/1)
Produce it with the Snowflake query described in SKILL.md Step 3.
"""

import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

# ── Time windows ─────────────────────────────────────────────────────────────
TIME_WINDOWS = {
    '1d':  timedelta(days=1),
    '3d':  timedelta(days=3),
    '1w':  timedelta(weeks=1),
    '2w':  timedelta(weeks=2),
    '1m':  timedelta(days=30),
}

# ── Risk status values from the sheet that indicate fraud ────────────────────
BAD_RISK_STATUS_VALUES = {'Bad Payment', 'bad payment'}


def load_and_merge(sheet_path: str, sf_fraud_path: str) -> pd.DataFrame:
    sheet = pd.read_csv(sheet_path, dtype={'payment_id': str})
    sf    = pd.read_csv(sf_fraud_path, dtype={'payment_id': str})

    # Normalise payment_id
    sheet['payment_id'] = sheet['payment_id'].str.strip()
    sf['payment_id']    = sf['payment_id'].str.strip()

    merged = sheet.merge(
        sf[['payment_id', 'is_fraud']].rename(columns={'is_fraud': 'is_fraud_sf'}),
        on='payment_id', how='left'
    )
    merged['is_fraud_sf'] = merged['is_fraud_sf'].fillna(False).astype(bool)

    # Worst-case combined flag:
    #   risk_status_enum (sheet col 11): Good Payment | Bad Payment | Escalated | Off-rail
    #   "Bad Payment" → fraud signal from the sheet
    risk_bad = merged['risk_status_enum'].astype(str).str.strip().isin(BAD_RISK_STATUS_VALUES)
    merged['is_fraud'] = merged['is_fraud_sf'] | risk_bad

    return merged


def apply_exclusions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove unreviewed payments and 'No' review answers.

    Column 'reviewed' (sheet col 10) values:
      - 'Yes'  → reviewed and confirmed → keep
      - 'No'   → legacy auto-added rows (alerts logic changed) → exclude
      - empty  → not yet reviewed by Risk Intel → exclude
    """
    before = len(df)
    reviewed_col = 'reviewed'
    df = df[df[reviewed_col].notna()]
    df = df[df[reviewed_col].astype(str).str.strip() != '']
    df = df[df[reviewed_col].astype(str).str.strip().str.lower() != 'no']
    print(f"  Exclusions: {before} → {len(df)} rows after removing unreviewed/'No' rows")
    return df


def calculate(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate precision per alert per time window."""
    df = df.copy()
    # added_at is the scope column (sheet col 8: "Added At")
    df['added_at'] = pd.to_datetime(df['added_at'], errors='coerce', utc=True)

    # Explode comma-separated alerts into individual rows
    df['alert_list'] = df['alert_codes'].fillna('').astype(str).str.split(',')
    exploded = df.explode('alert_list')
    exploded['alert'] = exploded['alert_list'].str.strip()
    exploded = exploded[exploded['alert'] != '']

    now = datetime.now(tz=timezone.utc)
    results = []

    for window_name, delta in TIME_WINDOWS.items():
        cutoff = now - delta
        window = exploded[exploded['added_at'] >= cutoff]

        for alert, group in window.groupby('alert', sort=True):
            total = len(group)
            fraud = int(group['is_fraud'].sum())
            precision = round(fraud / total * 100, 2) if total > 0 else 0.0
            results.append({
                'time_window':     window_name,
                'alert':           alert,
                'total_payments':  total,
                'fraud_payments':  fraud,
                'precision_pct':   precision,
            })
        print(f"  {window_name}: {len(window['alert'].unique())} unique alerts, "
              f"{len(window)} total payment-alert pairs")

    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser(description='Calculate alert precision')
    parser.add_argument('--sheet',    required=True, help='sheet_data.csv from parse_sheet.py')
    parser.add_argument('--sf-fraud', required=True, help='snowflake_fraud.csv (payment_id, is_fraud)')
    parser.add_argument('--output',   default='/tmp/risk_intel/precision.csv')
    args = parser.parse_args()

    print("Loading data...")
    try:
        df = load_and_merge(args.sheet, args.sf_fraud)
    except FileNotFoundError as e:
        sys.exit(f"ERROR: {e}")

    print(f"  Total sheet rows: {len(df)}")
    print(f"  Matched to Snowflake: {df['is_fraud_sf'].sum()} fraud from SF")
    print(f"  Fraud from risk_status col: {(df['is_fraud'] & ~df['is_fraud_sf']).sum()} additional")

    print("\nApplying exclusions...")
    df = apply_exclusions(df)

    print("\nCalculating precision per window...")
    results = calculate(df)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(args.output, index=False)

    print(f"\nDone. {len(results)} rows written to {args.output}")
    print("\nAlerts above 10% precision threshold (any window):")
    above = results[results['precision_pct'] >= 10].groupby('alert')['precision_pct'].max()
    if above.empty:
        print("  (none)")
    else:
        print(above.sort_values(ascending=False).to_string())


if __name__ == '__main__':
    main()
