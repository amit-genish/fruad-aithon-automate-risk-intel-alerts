#!/usr/bin/env python3
"""
Risk Alert Aggregation Script
==============================
Analyzes how effective each risk alert is at catching bad payments.

For each time window (1d, 3d, 1w, 1m), produces:
- Per individual alert: total / good / bad payment counts and % bad
- Per joined alert combo: same, for each unique alert combination that
  appeared together on a single payment row

Bad payment logic (worst case across two sources):
- Main table: Risk column == "Bad Payment"
- Snowflake PROD.ANALYTICS.RISK_PAYMENTS: MO_LABEL in ('SF', 'ATO')
A payment is bad if EITHER source says so.

Usage:
    python analyze_alerts.py <csv_path> [options]
    python analyze_alerts.py alerts.csv --sf-account xy12345.us-east-1 --sf-user myuser --sf-password mypass

Run with --print-columns first to confirm column names before a full run.
"""

import argparse
import base64
import sys
import os
from datetime import datetime, timezone, timedelta

import pandas as pd

# ── Bad status definitions ────────────────────────────────────────────────────
BAD_RISK_VALUES = {"Bad Payment"}  # Values in the Risk column that mean bad
BAD_MO_LABELS = {"SF", "ATO"}     # MO_LABEL values in Snowflake that mean bad

# ── Time windows ─────────────────────────────────────────────────────────────
TIME_WINDOWS = {
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(weeks=1),
    "1m": timedelta(days=30),
}


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def load_alerts_csv(path: str) -> pd.DataFrame:
    """Load the main alerts table from a CSV file."""
    df = pd.read_csv(path, low_memory=False)
    df.columns = df.columns.str.strip()
    return df


def load_snowflake_risk(account: str, user: str, password: str,
                         warehouse: str = "COMPUTE_WH",
                         database: str = "PROD",
                         schema: str = "ANALYTICS",
                         role: str = None) -> pd.DataFrame:
    """
    Load payment risk labels from Snowflake.
    Returns a DataFrame with columns: PAYMENT_ID, MO_LABEL
    """
    try:
        import snowflake.connector
    except ImportError:
        sys.exit(
            "ERROR: snowflake-connector-python not installed.\n"
            "Run: pip install snowflake-connector-python --break-system-packages"
        )

    connect_kwargs = dict(
        account=account,
        user=user,
        password=password,
        warehouse=warehouse,
        database=database,
        schema=schema,
    )
    if role:
        connect_kwargs["role"] = role

    print(f"  Connecting to Snowflake account: {account}...")
    conn = snowflake.connector.connect(**connect_kwargs)
    try:
        query = f"SELECT PAYMENT_ID, MO_LABEL FROM {database}.{schema}.RISK_PAYMENTS"
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Status determination (worst case)
# ─────────────────────────────────────────────────────────────────────────────

def merge_and_classify(
    alerts_df: pd.DataFrame,
    risk_df: pd.DataFrame,
    payment_id_col: str,
    risk_col: str,
    created_at_col: str,
    alerts_col: str,
) -> pd.DataFrame:
    """
    Join the alerts table with the Snowflake risk table and apply worst-case
    classification: a payment is BAD if either source says so.

    Returns the merged DataFrame with added columns:
      _is_bad       bool   True if payment is bad
      _created_at   datetime  parsed timestamp
    """
    df = alerts_df.copy()

    # Normalise join key
    df["_pid"] = df[payment_id_col].astype(str).str.strip()

    # Bad from main table
    df["_bad_main"] = df[risk_col].astype(str).str.strip().isin(BAD_RISK_VALUES)

    # Prepare Snowflake side
    risk_df = risk_df.copy()
    risk_df.columns = risk_df.columns.str.upper()
    risk_df["_pid"] = risk_df["PAYMENT_ID"].astype(str).str.strip()
    risk_df["_bad_sf"] = risk_df["MO_LABEL"].astype(str).str.strip().isin(BAD_MO_LABELS)

    # Left-join (keep all alert rows; payments not in Snowflake are not flagged bad from SF)
    merged = df.merge(
        risk_df[["_pid", "_bad_sf"]],
        on="_pid",
        how="left",
    )
    merged["_bad_sf"] = merged["_bad_sf"].fillna(False)

    # Worst case
    merged["_is_bad"] = merged["_bad_main"] | merged["_bad_sf"]

    # Parse timestamp
    merged["_created_at"] = pd.to_datetime(merged[created_at_col], errors="coerce", utc=True)

    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────

def aggregate_window(df: pd.DataFrame, alerts_col: str,
                      window_name: str, cutoff: datetime) -> list[dict]:
    """
    For one time window, compute per-alert stats.
    Returns a list of result row dicts.
    """
    window_df = df[df["_created_at"] >= cutoff].copy()
    n_total = len(window_df)
    print(f"    {window_name}: {n_total} payments in window")

    results = []

    # ── Individual alerts (split comma-separated) ──────────────────────────
    window_df["_alert_list"] = (
        window_df[alerts_col].fillna("").astype(str).str.split(",")
    )
    exploded = window_df.explode("_alert_list")
    exploded["_alert_key"] = exploded["_alert_list"].str.strip()
    exploded = exploded[exploded["_alert_key"] != ""]

    for alert, grp in exploded.groupby("_alert_key", sort=True):
        total = len(grp)
        bad = int(grp["_is_bad"].sum())
        good = total - bad
        results.append(_make_row(window_name, "individual", alert, total, good, bad))

    # ── Joined alert combos (alerts as they appear together) ──────────────
    window_df["_alert_combo"] = (
        window_df[alerts_col].fillna("").astype(str).str.strip()
    )
    combo_df = window_df[window_df["_alert_combo"] != ""]

    for combo, grp in combo_df.groupby("_alert_combo", sort=True):
        total = len(grp)
        bad = int(grp["_is_bad"].sum())
        good = total - bad
        results.append(_make_row(window_name, "joined", combo, total, good, bad))

    return results


def _make_row(window, alert_type, alert, total, good, bad) -> dict:
    pct_bad = round(bad / total * 100, 2) if total > 0 else 0.0
    return {
        "time_window": window,
        "alert_type": alert_type,
        "alert": alert,
        "total_payments": total,
        "good_payments": good,
        "bad_payments": bad,
        "pct_bad": pct_bad,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Aggregate risk alert effectiveness across time windows."
    )
    parser.add_argument("csv_path", help="Path to the alerts table CSV")
    parser.add_argument(
        "--print-columns", action="store_true",
        help="Print column names from the CSV and exit (use before first real run)"
    )

    # Column configuration
    col = parser.add_argument_group("Column names")
    col.add_argument("--payment-id-col", default="Payment ID",
                     help="Column with unique payment ID (default: 'Payment ID')")
    col.add_argument("--risk-col", default="Risk",
                     help="Column with risk status from main table (default: 'Risk')")
    col.add_argument("--alerts-col", default="Alert Codes",
                     help="Column with comma-separated alert codes (default: 'Alert Codes')")
    col.add_argument("--created-at-col", default="Created At",
                     help="Column with payment creation timestamp (default: 'Created At')")

    # Snowflake
    sf = parser.add_argument_group("Snowflake connection")
    sf.add_argument("--sf-account", default=os.environ.get("SF_ACCOUNT", ""),
                    help="Snowflake account identifier (or set SF_ACCOUNT env var)")
    sf.add_argument("--sf-user", default=os.environ.get("SF_USER", ""),
                    help="Snowflake username (or set SF_USER env var)")
    sf.add_argument("--sf-password", default=os.environ.get("SF_PASSWORD", ""),
                    help="Snowflake password (or set SF_PASSWORD env var)")
    sf.add_argument("--sf-warehouse", default="COMPUTE_WH")
    sf.add_argument("--sf-database", default="PROD")
    sf.add_argument("--sf-schema", default="ANALYTICS")
    sf.add_argument("--sf-role", default=None)
    sf.add_argument("--skip-snowflake", action="store_true",
                    help="Skip Snowflake join; classify bad using main table only")

    # Output
    parser.add_argument(
        "--output", default=f"alert_aggregation_{datetime.now().strftime('%Y%m%d')}.csv",
        help="Output CSV path"
    )

    args = parser.parse_args()

    # ── Load CSV ──────────────────────────────────────────────────────────────
    print(f"Loading alerts CSV: {args.csv_path}")
    try:
        alerts_df = load_alerts_csv(args.csv_path)
    except FileNotFoundError:
        sys.exit(f"ERROR: File not found: {args.csv_path}")

    print(f"  Rows: {len(alerts_df):,}  |  Columns: {len(alerts_df.columns)}")

    # ── Column check mode ─────────────────────────────────────────────────────
    if args.print_columns:
        print("\nColumn names in CSV:")
        for i, col in enumerate(alerts_df.columns, 1):
            print(f"  {i:3}. {col!r}")
        print("\nFirst row sample:")
        if len(alerts_df) > 0:
            for col in alerts_df.columns:
                print(f"  {col!r}: {alerts_df[col].iloc[0]!r}")
        return

    # ── Validate required columns ─────────────────────────────────────────────
    required = {
        args.payment_id_col: "--payment-id-col",
        args.risk_col: "--risk-col",
        args.alerts_col: "--alerts-col",
        args.created_at_col: "--created-at-col",
    }
    missing = {v: k for k, v in required.items() if k not in alerts_df.columns}
    if missing:
        print("\nERROR: These columns were not found in the CSV:")
        for arg_flag, col_name in missing.items():
            print(f"  {arg_flag!r} (configured as {col_name!r})")
        print("\nRun with --print-columns to see available columns.")
        sys.exit(1)

    # ── Load Snowflake ────────────────────────────────────────────────────────
    if args.skip_snowflake:
        print("Skipping Snowflake join (--skip-snowflake)")
        risk_df = pd.DataFrame(columns=["PAYMENT_ID", "MO_LABEL"])
    else:
        if not args.sf_account or not args.sf_user or not args.sf_password:
            sys.exit(
                "ERROR: Snowflake credentials required.\n"
                "Provide --sf-account, --sf-user, --sf-password  OR\n"
                "set env vars SF_ACCOUNT, SF_USER, SF_PASSWORD  OR\n"
                "use --skip-snowflake to run on main table data only."
            )
        print("Loading Snowflake risk payments...")
        risk_df = load_snowflake_risk(
            account=args.sf_account,
            user=args.sf_user,
            password=args.sf_password,
            warehouse=args.sf_warehouse,
            database=args.sf_database,
            schema=args.sf_schema,
            role=args.sf_role,
        )
        print(f"  Loaded {len(risk_df):,} Snowflake rows")

    # ── Join & classify ───────────────────────────────────────────────────────
    print("Joining tables and classifying payments (worst-case)...")
    merged = merge_and_classify(
        alerts_df=alerts_df,
        risk_df=risk_df,
        payment_id_col=args.payment_id_col,
        risk_col=args.risk_col,
        created_at_col=args.created_at_col,
        alerts_col=args.alerts_col,
    )

    bad_main = merged["_bad_main"].sum()
    bad_sf = merged["_bad_sf"].sum()
    bad_total = merged["_is_bad"].sum()
    print(f"  Bad from main table: {bad_main:,}")
    print(f"  Bad from Snowflake:  {bad_sf:,}")
    print(f"  Bad total (union):   {bad_total:,}  ({bad_total/len(merged)*100:.1f}%)")

    # ── Aggregate per time window ─────────────────────────────────────────────
    now = datetime.now(tz=timezone.utc)
    all_results = []

    print("Aggregating by time window...")
    for window_name, delta in TIME_WINDOWS.items():
        cutoff = now - delta
        rows = aggregate_window(merged, args.alerts_col, window_name, cutoff)
        all_results.extend(rows)

    # ── Export ────────────────────────────────────────────────────────────────
    results_df = pd.DataFrame(all_results)
    results_df.to_csv(args.output, index=False)
    print(f"\nResults saved to: {args.output}")
    print(f"Total result rows: {len(results_df):,}")

    # Summary table
    summary = (
        results_df.groupby(["time_window", "alert_type"])
        .agg(
            alerts=("alert", "nunique"),
            total_payments=("total_payments", "sum"),
            bad_payments=("bad_payments", "sum"),
        )
        .reset_index()
    )
    summary["pct_bad"] = (summary["bad_payments"] / summary["total_payments"] * 100).round(2)
    print("\nSummary:")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
