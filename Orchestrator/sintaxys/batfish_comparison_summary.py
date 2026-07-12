#!/usr/bin/env python3
"""
Parse comparison JSON predictions, evaluate them with Batfish, and write one
summary CSV.

Input:  JSON files in sintaxys/comparacion enfoque/, each with a predictions list.
Output: one summary CSV with one row per JSON/model.

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
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "Base_datasetmodificado"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "Base_datasetmodificado.csv"

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
            return VerificationResult(0.0, 0, 0, {})

        total_modified_files = count_modified_files(prediction_text)
        if total_modified_files == 0:
            return VerificationResult(0.0, 0, 0, {})

        temp_root = tempfile.mkdtemp(prefix="snapshot_verify_")
        try:
            zip_path = self.create_snapshot_from_string(prediction_text, base_dir=temp_root)
            if not zip_path:
                return VerificationResult(0.0, total_modified_files, 0, {})

            batfish_server = self.mcp_client.server("batfish")
            batfish_server.call_tool("load_snapshot", {"zip_path": zip_path})
            status_response = batfish_server.call_tool("file_parse_status", {})
            results = extract_file_parse_results(status_response)

            scores: List[float] = []
            status_counts: Dict[str, int] = {}
            for item in results:
                normalized = normalize_status(item.get("status"))
                status_counts[normalized] = status_counts.get(normalized, 0) + 1
                scores.append(status_to_score(normalized))

            pqs_modified = sum(scores) / total_modified_files if total_modified_files else 0.0
            return VerificationResult(
                float(pqs_modified),
                total_modified_files,
                len(results),
                status_counts,
            )
        except Exception:
            return VerificationResult(0.0, total_modified_files, 0, {})
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


def summarize_model(
    model_name: str,
    predictions: List[Any],
    parser: BatfishPredictionParser,
    evaluator: BatfishPQSEvaluator,
) -> Dict[str, Any]:
    print(f"[{model_name}] Evaluating {len(predictions)} predictions...")

    pqs_values: List[float] = []
    total_modified_files = 0
    total_passed = 0
    total_partially_parsed = 0
    total_failed = 0
    total_unknown = 0

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

        pqs_values.append(score_sum / accounted_files if accounted_files else 0.0)
        total_modified_files += accounted_files
        total_passed += result.parse_status_counts.get("PASSED", 0)
        total_partially_parsed += result.parse_status_counts.get("PARTIALLY_PARSED", 0)
        total_failed += result.parse_status_counts.get("FAILED", 0)
        total_unknown += (
            result.parse_status_counts.get("UNKNOWN", 0) + parser_unknown_files
        )

    print(f"[{model_name}] Done")

    return {
        "model_name": model_name,
        "rows": len(predictions),
        "avg_pqs_modified": mean(pqs_values) if pqs_values else 0.0,
        "median_pqs_modified": median(pqs_values) if pqs_values else 0.0,
        "total_modified_files": total_modified_files,
        "total_passed": total_passed,
        "total_partially_parsed": total_partially_parsed,
        "total_failed": total_failed,
        "total_unknown": total_unknown,
    }


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

    try:
        rows = [
            summarize_model(model_name, predictions, parser, evaluator)
            for json_path in json_files
            for model_name, predictions in load_model_predictions(json_path)
        ]
    finally:
        print("Closing MCP connections...")
        mcp_client.close_all()

    fieldnames = [
        "model_name",
        "rows",
        "avg_pqs_modified",
        "median_pqs_modified",
        "total_modified_files",
        "total_passed",
        "total_partially_parsed",
        "total_failed",
        "total_unknown",
    ]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Summary written to: {output_csv}")


if __name__ == "__main__":
    main()
