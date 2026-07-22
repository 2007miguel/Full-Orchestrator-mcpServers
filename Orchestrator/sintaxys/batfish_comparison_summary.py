#!/usr/bin/env python3
"""
Parse comparison JSON predictions, evaluate them with Batfish, and write one
summary CSV.

Input:  JSON files in an input dir, each with a predictions list or a results
        list (several models per file).
Output: one summary CSV with one row per JSON/model. Each row reports, at the
        PER-QUESTION level (worst-status-wins verdict), how many predictions were
        correct / partially_correct / error(=failed+unknown), as counts and %.

NOTE: this harness used to embed its own copy of ``BatfishPredictionParser``.
It now imports the single source of truth from ``utils/batfish_parser.py`` so
the two never diverge again.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "v 16"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "base.json_summary.csv"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Single source of truth for the parser (shared with the orchestrator pipeline).
from utils.batfish_parser import BatfishPredictionParser, PROMPT_RE


FULL_BLOCK_RE = re.compile(
    r"\[configs/(?P<name>[^\]]+\.cfg)\]\s*\n?(?P<body>.*?)\n?\[/configs/(?P=name)\]",
    re.IGNORECASE | re.DOTALL,
)

STATUS_SCORE_MAP = {
    "PASSED": 1.0,
    "FAILED": 0.0,
    "UNKNOWN": 0.0,
}


@dataclass
class VerificationResult:
    pqs_modified: float
    total_modified_files: int
    parsed_files: int
    parse_status_counts: Dict[str, int]
    files: List[Dict[str, str]] = field(default_factory=list)
    note: str = ""


def create_project_mcp_client() -> Any:
    import mcp_client

    manager_class = getattr(mcp_client, "MCPClientManager", None)
    servers_config = getattr(mcp_client, "SERVERS", None)
    if not (manager_class and callable(manager_class)):
        raise AttributeError("Could not find MCPClientManager in mcp_client.py")
    if not isinstance(servers_config, dict):
        raise AttributeError("Could not find SERVERS in mcp_client.py")
    return manager_class(servers_config)


def count_modified_files(text: str) -> int:
    return sum(1 for _ in FULL_BLOCK_RE.finditer(text))


def count_expected_files(prediction: Any, parser: BatfishPredictionParser) -> int:
    text = parser._normalize_text(prediction)
    devices = {
        match.group(1)
        for line in text.split("\n")
        if (match := PROMPT_RE.match(line))
    }
    return max(len(devices), 1)


def normalize_status(status: Any) -> str:
    if status is None:
        return "UNKNOWN"
    normalized = str(status).strip().upper().replace("-", "_").replace(" ", "_")
    if "PARTIAL" in normalized:
        return "PARTIALLY_PARSED"
    return normalized or "UNKNOWN"


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
        try:
            return json.loads(response.strip())
        except Exception:
            return response
    if isinstance(response, dict):
        if "structuredContent" in response:
            return response["structuredContent"]
        if "content" in response and isinstance(response["content"], list):
            for item in response["content"]:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        try:
                            return json.loads(text)
                        except Exception:
                            pass
        return response
    return response


def extract_file_parse_results(status_response: Any) -> List[Dict[str, Any]]:
    payload = unwrap_mcp_response(status_response)
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            return [item for item in payload["results"] if isinstance(item, dict)]
        for key in ("data", "result", "payload"):
            inner = payload.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("results"), list):
                return [item for item in inner["results"] if isinstance(item, dict)]
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


class BatfishPQSEvaluator:
    def __init__(self, mcp_client: Any):
        self.mcp_client = mcp_client
        from utils.create_snapshot import create_snapshot_from_string

        self.create_snapshot_from_string = create_snapshot_from_string

    def verify_prediction(self, prediction_text: str) -> VerificationResult:
        if not prediction_text or prediction_text == "None":
            return VerificationResult(0.0, 0, 0, {}, [], "parser_returned_none")

        total_modified_files = count_modified_files(prediction_text)
        if total_modified_files == 0:
            return VerificationResult(0.0, 0, 0, {}, [], "no_config_blocks")

        temp_root = tempfile.mkdtemp(prefix="snapshot_verify_")
        try:
            zip_path = self.create_snapshot_from_string(prediction_text, base_dir=temp_root)
            if not zip_path:
                return VerificationResult(
                    0.0, total_modified_files, 0, {}, [], "snapshot_not_created"
                )

            batfish_server = self.mcp_client.server("batfish")
            batfish_server.call_tool("load_snapshot", {"zip_path": zip_path})
            status_response = batfish_server.call_tool("file_parse_status", {})
            results = extract_file_parse_results(status_response)

            scores: List[float] = []
            status_counts: Dict[str, int] = {}
            files: List[Dict[str, str]] = []
            for item in results:
                normalized = normalize_status(item.get("status"))
                status_counts[normalized] = status_counts.get(normalized, 0) + 1
                scores.append(status_to_score(normalized))
                files.append(
                    {
                        "file_name": str(item.get("file_name") or ""),
                        "status": normalized,
                        "error": str(item.get("error") or ""),
                    }
                )

            pqs_modified = sum(scores) / total_modified_files if total_modified_files else 0.0
            return VerificationResult(
                float(pqs_modified),
                total_modified_files,
                len(results),
                status_counts,
                files,
                "",
            )
        except Exception as exc:
            return VerificationResult(
                0.0,
                total_modified_files,
                0,
                {},
                [],
                f"error:{type(exc).__name__}: {exc}"[:200],
            )
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


def verdict_for(counts: Dict[str, int], unknown_files: int) -> str:
    """Worst status wins, so a prediction is only PASSED when every file parsed."""
    if counts.get("FAILED", 0):
        return "FAILED"
    if counts.get("UNKNOWN", 0) or unknown_files:
        return "UNKNOWN"
    if counts.get("PARTIALLY_PARSED", 0):
        return "PARTIALLY_PARSED"
    if counts.get("PASSED", 0):
        return "PASSED"
    return "UNKNOWN"


def summarize_model(
    model_name: str,
    predictions: List[Any],
    parser: BatfishPredictionParser,
    evaluator: BatfishPQSEvaluator,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    print(f"[{model_name}] Evaluating {len(predictions)} predictions...")

    pqs_values: List[float] = []
    total_modified_files = 0
    total_passed = 0
    total_partially_parsed = 0
    total_failed = 0
    total_unknown = 0
    # Per-prediction verdict tally (one verdict per question, worst-status-wins).
    verdict_counts: Dict[str, int] = {
        "PASSED": 0,
        "PARTIALLY_PARSED": 0,
        "FAILED": 0,
        "UNKNOWN": 0,
    }
    details: List[Dict[str, Any]] = []

    for index, prediction in enumerate(predictions, start=1):
        if index == 1 or index == len(predictions) or index % 10 == 0:
            print(f"[{model_name}] {index}/{len(predictions)}")

        parsed_config = parser.parse_prediction(prediction)
        result = evaluator.verify_prediction(parsed_config)

        expected_files = count_expected_files(prediction, parser)
        parser_unknown_files = max(
            expected_files - result.total_modified_files,
            0,
        )
        accounted_files = result.total_modified_files + parser_unknown_files
        score_sum = result.pqs_modified * result.total_modified_files

        pqs = score_sum / accounted_files if accounted_files else 0.0
        pqs_values.append(pqs)
        total_modified_files += accounted_files
        total_passed += result.parse_status_counts.get("PASSED", 0)
        total_partially_parsed += result.parse_status_counts.get("PARTIALLY_PARSED", 0)
        total_failed += result.parse_status_counts.get("FAILED", 0)
        total_unknown += (
            result.parse_status_counts.get("UNKNOWN", 0) + parser_unknown_files
        )

        verdict = verdict_for(result.parse_status_counts, parser_unknown_files)
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        details.append(
            {
                "model_name": model_name,
                "prediction": index,
                "verdict": verdict,
                "pqs": round(pqs, 4),
                "files": accounted_files,
                "passed": result.parse_status_counts.get("PASSED", 0),
                "partially_parsed": result.parse_status_counts.get("PARTIALLY_PARSED", 0),
                "failed": result.parse_status_counts.get("FAILED", 0),
                "unknown": (
                    result.parse_status_counts.get("UNKNOWN", 0) + parser_unknown_files
                ),
                "file_statuses": "; ".join(
                    f"{item['file_name']}={item['status']}" for item in result.files
                ),
                "note": result.note,
            }
        )

        if verdict != "PASSED":
            suffix = f" ({result.note})" if result.note else ""
            print(f"[{model_name}]   #{index}: {verdict}{suffix}")

    print(f"[{model_name}] Done")

    total = len(predictions)
    correct = verdict_counts["PASSED"]
    partially_correct = verdict_counts["PARTIALLY_PARSED"]
    # error/unknown/failed van juntos en un solo cubo, como pediste.
    error = verdict_counts["FAILED"] + verdict_counts["UNKNOWN"]

    def pct(n: int) -> float:
        return round(100.0 * n / total, 1) if total else 0.0

    return (
        {
            "model_name": model_name,
            "total": total,
            "correct": correct,
            "partially_correct": partially_correct,
            "error": error,
            "pct_correct": pct(correct),
            "pct_partially_correct": pct(partially_correct),
            "pct_error": pct(error),
        },
        details,
    )


def load_model_predictions(json_path: Path) -> List[Tuple[str, List[Any]]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    predictions = data.get("predictions")
    if isinstance(predictions, list):
        return [(json_path.stem, predictions)]

    results = data.get("results")
    if isinstance(results, list):
        model_predictions: List[Tuple[str, List[Any]]] = []
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                raise ValueError(f"{json_path} results[{index}] is not an object")

            model_name = result.get("model_name")
            predictions = result.get("predictions")
            if not isinstance(model_name, str) or not model_name.strip():
                raise ValueError(
                    f"{json_path} results[{index}] does not contain a model_name"
                )
            if not isinstance(predictions, list):
                raise ValueError(
                    f"{json_path} results[{index}] does not contain a predictions list"
                )
            model_predictions.append((model_name, predictions))

        return model_predictions

    raise ValueError(
        f"{json_path} does not contain a predictions list or a results list"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate Batfish syntax summary for comparison JSON files."
    )
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_CSV))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir)
    output_csv = Path(args.output)

    print(f"Reading JSON files from: {input_dir}")
    json_files = sorted(input_dir.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"No JSON files found in: {input_dir}")
    print(f"Found {len(json_files)} JSON files")

    print("Starting Batfish MCP server...")
    mcp_client = create_project_mcp_client()
    batfish_server = mcp_client.server("batfish")
    batfish_server.start()
    batfish_server.initialize()
    print("Batfish MCP server ready")

    parser = BatfishPredictionParser()
    evaluator = BatfishPQSEvaluator(mcp_client)

    rows: List[Dict[str, Any]] = []
    all_details: List[Dict[str, Any]] = []
    try:
        for json_path in json_files:
            # Etiqueta del archivo (p.ej. int4/int8/normal): sin esto, los mismos
            # model_name internos de cada JSON producirian filas indistinguibles.
            source = json_path.stem
            for model_name, predictions in load_model_predictions(json_path):
                row, details = summarize_model(
                    model_name, predictions, parser, evaluator
                )
                row = {"source": source, **row}
                for d in details:
                    d["source"] = source
                rows.append(row)
                all_details.extend(details)
    finally:
        print("Closing MCP connections...")
        mcp_client.close_all()

    fieldnames = [
        "source",
        "model_name",
        "total",
        "correct",
        "partially_correct",
        "error",
        "pct_correct",
        "pct_partially_correct",
        "pct_error",
    ]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Summary written to: {output_csv}")

    detail_csv = output_csv.with_name(f"{output_csv.stem}_detail.csv")
    detail_fieldnames = [
        "source",
        "model_name",
        "prediction",
        "verdict",
        "pqs",
        "files",
        "passed",
        "partially_parsed",
        "failed",
        "unknown",
        "file_statuses",
        "note",
    ]
    with detail_csv.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=detail_fieldnames)
        writer.writeheader()
        writer.writerows(all_details)

    print(f"Per-prediction detail written to: {detail_csv}")

    for row in rows:
        source = row["source"]
        model_name = row["model_name"]
        model_details = [
            d for d in all_details
            if d["source"] == source and d["model_name"] == model_name
        ]
        total = row["total"]
        print(f"\n=== {source} / {model_name}  (n={total}) ===")
        print(
            f"  correct            {row['correct']:>3}  ({row['pct_correct']:>5}%)"
        )
        print(
            f"  partially_correct  {row['partially_correct']:>3}  "
            f"({row['pct_partially_correct']:>5}%)"
        )
        print(
            f"  error/unknown/fail {row['error']:>3}  ({row['pct_error']:>5}%)"
        )
        # Que preguntas cayeron en cada cubo no-correcto (numero de fila 1..n).
        for verdict, label in (
            ("PARTIALLY_PARSED", "partial"),
            ("FAILED", "failed"),
            ("UNKNOWN", "unknown"),
        ):
            hits = [d["prediction"] for d in model_details if d["verdict"] == verdict]
            if hits:
                print(f"    {label:<8}: {hits}")


if __name__ == "__main__":
    main()
