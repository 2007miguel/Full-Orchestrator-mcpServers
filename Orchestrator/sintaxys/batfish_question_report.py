"""
Per-question Batfish report, printed to the console.

For every prediction in a predictions JSON (``base.json`` / ``rag.json``) it shows:
  - the question number as it appears in the eval CSV (``retrieved_logs[i].id``)
  - the question itself (``requirement``)
  - its score (same PQS math as batfish_comparison_summary.py, so numbers match the CSV)
  - the Batfish report when it did not parse cleanly: which file, which line, why

Usage:
    python3 Orchestrator/sintaxys/batfish_question_report.py --json "Orchestrator/sintaxys/v 16/base.json"
    python3 Orchestrator/sintaxys/batfish_question_report.py --json ".../base.json" --only-failed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from utils.batfish_parser import BatfishPredictionParser

# Reuse the scoring path of the summary script so console and CSV never disagree.
from batfish_comparison_summary import (
    BatfishPQSEvaluator,
    count_expected_files,
    create_project_mcp_client,
    unwrap_mcp_response,
    verdict_for,
)

MARK = {
    "PASSED": "OK  ",
    "PARTIALLY_PARSED": "~   ",
    "FAILED": "FAIL",
    "UNKNOWN": "?   ",
}


def extract_warnings(response: Any) -> List[Dict[str, Any]]:
    payload = unwrap_mcp_response(response)
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return [item for item in payload["results"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def summarize_batfish_error(error: str) -> List[str]:
    """Pull the useful lines out of Batfish's work log, which is mostly JSON noise.

    A FAILED snapshot is not always a syntax error: Batfish reports "Parsing...OK" and
    then crashes while converting the config, and the two mean very different things.
    """
    if not error:
        return []

    lines: List[str] = []
    parsed_ok = "Parsing...OK" in error

    # Batfish writes it as "Exception in container:...; exception:java.lang.Foo: msg".
    for raw in error.splitlines():
        marker = raw.rfind("exception:")
        if marker != -1:
            lines.append(raw[marker + len("exception:"):].strip())
            break

    if parsed_ok:
        lines.append(
            "la sintaxis SI parseo (Parsing...OK); Batfish fallo al convertir el config"
        )
    elif not lines:
        lines.append(error.splitlines()[0] if error else "(sin detalle)")

    return lines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print a per-question Batfish report for a predictions JSON."
    )
    parser.add_argument("--json", required=True, help="Path to base.json / rag.json")
    parser.add_argument(
        "--only-failed",
        action="store_true",
        help="Show only questions that did not parse cleanly",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Only the first N questions (0 = all)"
    )
    parser.add_argument(
        "--output",
        default="",
        help="Where to save if you confirm at the end (default: <json>_report.txt)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    json_path = Path(args.json)

    # Everything printed is also kept, so it can be saved at the end if the user says so.
    transcript: List[str] = []

    def emit(line: str = "") -> None:
        print(line, flush=True)
        transcript.append(line)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    predictions = data.get("predictions")
    if not isinstance(predictions, list):
        raise ValueError(f"{json_path} does not contain a predictions list")

    logs = data.get("retrieved_logs")
    logs = logs if isinstance(logs, list) else []

    if args.limit:
        predictions = predictions[: args.limit]

    emit(f"Questions from: {json_path}")
    print("Starting Batfish MCP server...", flush=True)
    mcp_client = create_project_mcp_client()
    batfish_server = mcp_client.server("batfish")
    batfish_server.start()
    batfish_server.initialize()
    print("Batfish MCP server ready\n", flush=True)

    parser = BatfishPredictionParser()
    evaluator = BatfishPQSEvaluator(mcp_client)

    shown = 0
    try:
        for index, prediction in enumerate(predictions, start=1):
            log = logs[index - 1] if index - 1 < len(logs) else {}
            csv_id = log.get("id", index - 1)
            requirement = str(log.get("requirement") or "(question not found in retrieved_logs)")

            config = parser.parse_prediction(prediction)
            result = evaluator.verify_prediction(config)

            expected_files = count_expected_files(prediction, parser)
            unknown_files = max(expected_files - result.total_modified_files, 0)
            accounted = result.total_modified_files + unknown_files
            score_sum = result.pqs_modified * result.total_modified_files
            pqs = score_sum / accounted if accounted else 0.0

            verdict = verdict_for(result.parse_status_counts, unknown_files)
            if args.only_failed and verdict == "PASSED":
                continue

            shown += 1
            emit("=" * 78)
            emit(
                f"[{MARK.get(verdict, '?')}] pregunta CSV #{csv_id}"
                f"  (prediccion {index}/{len(predictions)})"
                f"  |  {verdict}  |  PQS {pqs:.2f}"
            )
            emit(f"  Pregunta: {requirement}")

            emit("  Prediccion:")
            for line in str(parser._normalize_text(prediction)).splitlines():
                emit(f"    {line}")

            if verdict == "PASSED":
                continue

            emit("  Reporte Batfish:")
            if result.note:
                emit(f"    sin evaluar por Batfish -> {result.note}")

            for item in result.files:
                emit(f"    {item['file_name']}: {item['status']}")
                for detail in summarize_batfish_error(item.get("error", "")):
                    if detail:
                        emit(f"      {detail}")

            # Aggregating duplicates collapses the per-line rows and loses the line number.
            warnings = extract_warnings(
                batfish_server.call_tool("parse_warning", {"aggregate_duplicates": False})
            )
            for warn in warnings:
                filename = warn.get("filename", "")
                line_no = warn.get("line", "?")
                text = str(warn.get("text") or "").strip()
                comment = str(warn.get("comment") or "").strip()
                emit(f"      linea {line_no} de {filename}: {text!r}")
                if comment:
                    emit(f"        -> {comment}")
            if not warnings and not result.note and not result.files:
                emit("    (Batfish no devolvio detalle)")
    finally:
        print("\nClosing MCP connections...", flush=True)
        mcp_client.close_all()

    emit("=" * 78)
    emit(f"Mostradas {shown} preguntas.")

    save_path = Path(args.output) if args.output else json_path.with_name(
        f"{json_path.stem}_report.txt"
    )

    try:
        answer = input(f"\n¿Guardar el reporte en {save_path}? [y/N]: ").strip().lower()
    except EOFError:
        # Non-interactive (piped/redirected): saving needs an explicit yes, so don't.
        answer = ""

    if answer in {"y", "yes", "s", "si", "sí"}:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text("\n".join(transcript) + "\n", encoding="utf-8")
        print(f"Reporte guardado en: {save_path}")
    else:
        print("Reporte no guardado.")


if __name__ == "__main__":
    main()
