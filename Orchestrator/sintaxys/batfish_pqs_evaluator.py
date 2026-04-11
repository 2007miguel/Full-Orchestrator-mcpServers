#!/usr/bin/env python3
"""
Evaluate Modified Parse Quality Score (PQS_modified) for Batfish-ready predictions.

Expected project layout (example):
  /mcp_client.py           or /mcp_client/__init__.py
  /utils/create_snapshot.py
  /sintaxys/batfish_pqs_evaluator.py

The input CSV must contain at least these columns:
  - model_name
  - predictions

For each row, the script:
  1. Cleans the prediction text.
  2. Verifies that it contains bracketed config blocks such as:
       [configs/router1.cfg]
       ...
       [/configs/router1.cfg]
  3. Creates a Batfish snapshot ZIP from the prediction text only.
  4. Loads that snapshot through an MCP Batfish server.
  5. Runs file_parse_status.
  6. Maps each returned status to a numeric score:
       PASSED -> 1.0
       PARTIALLY_PARSED / PARTIAL* -> 0.5
       FAILED / UNKNOWN -> 0.0
  7. Computes row-level PQS_modified as:
       sum(file_scores) / total_modified_files
  8. Aggregates the mean PQS_modified by model_name.

Important:
- This script does NOT merge with a base snapshot.
- It preserves the same number of rows in the row-level output.
- If a row is invalid, empty, or does not contain usable config blocks,
  its PQS_modified is set to 0.0 and the row is still preserved.
- It imports the MCP client directly from the project root.

Outputs:
- <input_stem>_verified_rows.csv
- <input_stem>_verified_summary.csv
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


# ---------------------------------------------------------------------------
# Project root bootstrap
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Cleaning and validation helpers
# ---------------------------------------------------------------------------

FULL_BLOCK_RE = re.compile(
    r"\[configs/(?P<name>[^\]]+\.cfg)\]\s*\n?(?P<body>.*?)\n?\[/configs/(?P=name)\]",
    re.IGNORECASE | re.DOTALL,
)

INVALID_PREDICTION_MARKERS = [
    "<existing_",
    "<placeholder",
]


def clean_prediction_text(value: Any) -> str:
    """Normalize CSV cell content into a plain multiline string."""
    if value is None:
        return ""

    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    if not text:
        return ""

    if (
        (text.startswith('"') and text.endswith('"'))
        or (text.startswith("'") and text.endswith("'"))
    ):
        for loader in (json.loads, ast.literal_eval):
            try:
                unwrapped = loader(text)
                if isinstance(unwrapped, str):
                    text = unwrapped
                    break
            except Exception:
                continue

    text = (
        text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\'", "'")
    )

    return text.strip()


def has_usable_config_blocks(text: str) -> bool:
    if not text:
        return False
    if any(marker in text for marker in INVALID_PREDICTION_MARKERS):
        return False
    return bool(FULL_BLOCK_RE.search(text))


def count_modified_files(text: str) -> int:
    return sum(1 for _ in FULL_BLOCK_RE.finditer(text))


# ---------------------------------------------------------------------------
# Batfish/MCP response parsing
# ---------------------------------------------------------------------------

STATUS_SCORE_MAP = {
    "PASSED": 1.0,
    "FAILED": 0.0,
    "UNKNOWN": 0.0,
}


def normalize_status(status: Any) -> str:
    if status is None:
        return "UNKNOWN"
    s = str(status).strip().upper().replace("-", "_").replace(" ", "_")
    if "PARTIAL" in s:
        return "PARTIALLY_PARSED"
    if s in STATUS_SCORE_MAP:
        return s
    return s or "UNKNOWN"


def status_to_score(status: Any) -> float:
    normalized = normalize_status(status)
    if normalized == "PARTIALLY_PARSED":
        return 0.5
    return STATUS_SCORE_MAP.get(normalized, 0.0)


def unwrap_mcp_response(response: Any) -> Any:
    if response is None:
        return None
    if isinstance(response, (list, tuple)):
        return response
    if isinstance(response, str):
        text = response.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return text
    if isinstance(response, dict):
        if "structuredContent" in response:
            return response["structuredContent"]
        if "content" in response and isinstance(response["content"], list):
            for item in response["content"]:
                if isinstance(item, dict):
                    maybe_text = item.get("text") or item.get("content")
                    if isinstance(maybe_text, str):
                        try:
                            return json.loads(maybe_text)
                        except Exception:
                            continue
        return response
    return response


def extract_file_parse_results(status_response: Any) -> List[Dict[str, Any]]:
    payload = unwrap_mcp_response(status_response)
    if payload is None:
        return []
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            return [x for x in payload["results"] if isinstance(x, dict)]
        for key in ("data", "result", "payload"):
            inner = payload.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("results"), list):
                return [x for x in inner["results"] if isinstance(x, dict)]
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


# ---------------------------------------------------------------------------
# Project integration helpers
# ---------------------------------------------------------------------------


def create_project_mcp_client() -> Any:
    """
    Import the MCP client from the project root.

    This function is adapted to find and instantiate the `MCPClientManager`
    from the project's `mcp_client.py` file.

    Adjust this function if your project uses a different API.
    """
    import mcp_client

    manager_class = getattr(mcp_client, "MCPClientManager", None)
    if not (manager_class and callable(manager_class)):
        raise AttributeError(
            "Could not find 'MCPClientManager' class in 'mcp_client.py'."
        )

    servers_config = getattr(mcp_client, "SERVERS", None)
    if not isinstance(servers_config, dict):
        raise AttributeError(
            "Could not find 'SERVERS' dictionary in 'mcp_client.py'."
        )

    return manager_class(servers_config)


@dataclass
class VerificationResult:
    pqs_modified: float
    total_modified_files: int
    parsed_files: int
    parse_status_counts: Dict[str, int]
    verification_state: str
    error_message: str = ""


class BatfishPQSEvaluator:
    def __init__(self, mcp_client: Any):
        self.mcp_client = mcp_client

        from utils.create_snapshot import create_snapshot_from_string

        self.create_snapshot_from_string = create_snapshot_from_string

    def verify_prediction(self, prediction_text: str) -> VerificationResult:
        cleaned = clean_prediction_text(prediction_text)

        if not has_usable_config_blocks(cleaned):
            return VerificationResult(
                pqs_modified=0.0,
                total_modified_files=0,
                parsed_files=0,
                parse_status_counts={},
                verification_state="NO_VALID_CONFIG_BLOCKS",
            )

        temp_root = tempfile.mkdtemp(prefix="snapshot_verify_")

        try:
            zip_path = self.create_snapshot_from_string(cleaned, base_dir=temp_root)
            if not zip_path:
                return VerificationResult(
                    pqs_modified=0.0,
                    total_modified_files=count_modified_files(cleaned),
                    parsed_files=0,
                    parse_status_counts={},
                    verification_state="SNAPSHOT_CREATION_FAILED",
                    error_message="create_snapshot_from_string returned no zip path",
                )

            batfish_server = self.mcp_client.server("batfish")
            batfish_server.call_tool("load_snapshot", {"zip_path": zip_path})
            status_response = batfish_server.call_tool("file_parse_status", {})

            results = extract_file_parse_results(status_response)
            total_modified_files = count_modified_files(cleaned)

            if not results:
                return VerificationResult(
                    pqs_modified=0.0,
                    total_modified_files=total_modified_files,
                    parsed_files=0,
                    parse_status_counts={},
                    verification_state="EMPTY_FILE_PARSE_STATUS",
                    error_message="file_parse_status returned no parse results",
                )

            scores: List[float] = []
            status_counts: Dict[str, int] = {}
            for item in results:
                normalized = normalize_status(item.get("status"))
                status_counts[normalized] = status_counts.get(normalized, 0) + 1
                scores.append(status_to_score(normalized))

            parsed_files = len(results)
            denominator = total_modified_files if total_modified_files > 0 else parsed_files
            if denominator <= 0:
                denominator = parsed_files

            pqs_modified = sum(scores) / denominator if denominator > 0 else 0.0

            return VerificationResult(
                pqs_modified=float(pqs_modified),
                total_modified_files=total_modified_files,
                parsed_files=parsed_files,
                parse_status_counts=status_counts,
                verification_state="VERIFIED",
            )

        except Exception as exc:
            return VerificationResult(
                pqs_modified=0.0,
                total_modified_files=count_modified_files(cleaned),
                parsed_files=0,
                parse_status_counts={},
                verification_state="ERROR",
                error_message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                shutil.rmtree(temp_root, ignore_errors=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------


def build_outputs(
    input_df: pd.DataFrame,
    evaluator: BatfishPQSEvaluator,
    model_col: str,
    prediction_col: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    detailed_df = input_df.copy()

    pqs_values: List[float] = []
    total_modified_files_values: List[int] = []
    parsed_files_values: List[int] = []
    verification_states: List[str] = []
    error_messages: List[str] = []
    passed_counts: List[int] = []
    partial_counts: List[int] = []
    failed_counts: List[int] = []
    unknown_counts: List[int] = []

    for _, row in detailed_df.iterrows():
        result = evaluator.verify_prediction(row.get(prediction_col, ""))
        pqs_values.append(result.pqs_modified)
        total_modified_files_values.append(result.total_modified_files)
        parsed_files_values.append(result.parsed_files)
        verification_states.append(result.verification_state)
        error_messages.append(result.error_message)
        passed_counts.append(result.parse_status_counts.get("PASSED", 0))
        partial_counts.append(result.parse_status_counts.get("PARTIALLY_PARSED", 0))
        failed_counts.append(result.parse_status_counts.get("FAILED", 0))
        unknown_counts.append(result.parse_status_counts.get("UNKNOWN", 0))

    detailed_df["pqs_modified"] = pqs_values
    detailed_df["total_modified_files"] = total_modified_files_values
    detailed_df["parsed_files_reported_by_batfish"] = parsed_files_values
    detailed_df["verification_state"] = verification_states
    detailed_df["verification_error"] = error_messages
    detailed_df["count_passed"] = passed_counts
    detailed_df["count_partially_parsed"] = partial_counts
    detailed_df["count_failed"] = failed_counts
    detailed_df["count_unknown"] = unknown_counts

    summary_df = (
        detailed_df.groupby(model_col, dropna=False, as_index=False)
        .agg(
            rows=(model_col, "size"),
            avg_pqs_modified=("pqs_modified", "mean"),
            median_pqs_modified=("pqs_modified", "median"),
            total_modified_files=("total_modified_files", "sum"),
            total_passed=("count_passed", "sum"),
            total_partially_parsed=("count_partially_parsed", "sum"),
            total_failed=("count_failed", "sum"),
            total_unknown=("count_unknown", "sum"),
        )
        .sort_values(["avg_pqs_modified", model_col], ascending=[False, True])
        .reset_index(drop=True)
    )

    return detailed_df, summary_df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute row-level and per-model Batfish Modified Parse Quality Score (PQS_modified)."
    )
    parser.add_argument("input_csv", help="CSV file with at least model_name and predictions columns")
    parser.add_argument(
        "--model-column",
        default="model_name",
        help="Column containing the model name (default: model_name)",
    )
    parser.add_argument(
        "--prediction-column",
        default="predictions",
        help="Column containing bracketed Batfish-ready predictions (default: predictions)",
    )
    parser.add_argument(
        "--rows-output",
        default=None,
        help="Optional output path for row-level results CSV",
    )
    parser.add_argument(
        "--summary-output",
        default=None,
        help="Optional output path for per-model summary CSV",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    input_path = Path(args.input_csv)
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    df = pd.read_csv(input_path, keep_default_na=False)

    required_columns = {args.model_column, args.prediction_column}
    missing = required_columns.difference(df.columns)
    if missing:
        raise ValueError(f"Missing required columns in CSV: {sorted(missing)}")

    mcp_client = create_project_mcp_client()

    # Start and initialize the batfish server session once.
    batfish_server = mcp_client.server("batfish")
    batfish_server.start()
    batfish_server.initialize()

    evaluator = BatfishPQSEvaluator(mcp_client)

    detailed_df, summary_df = build_outputs(
        input_df=df,
        evaluator=evaluator,
        model_col=args.model_column,
        prediction_col=args.prediction_column,
    )

    rows_output = (
        Path(args.rows_output)
        if args.rows_output
        else input_path.with_name(f"{input_path.stem}_verified_rows.csv")
    )
    summary_output = (
        Path(args.summary_output)
        if args.summary_output
        else input_path.with_name(f"{input_path.stem}_verified_summary.csv")
    )

    detailed_df.to_csv(rows_output, index=False)
    summary_df.to_csv(summary_output, index=False)

    print(f"Project root added to sys.path: {PROJECT_ROOT}")
    print(f"Row-level results written to: {rows_output}")
    print(f"Per-model summary written to: {summary_output}")

    # Cleanly close server connections
    mcp_client.close_all()


if __name__ == "__main__":
    main()
