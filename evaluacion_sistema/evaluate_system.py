import argparse
import csv
import statistics
import sys
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

# This script lives in evaluacion_sistema/; the actual code lives under Orchestrator/.
EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
PROJECT_ROOT = REPO_ROOT / "Orchestrator"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.execution_controller import ExecutionController
from core.prompt_manager import PromptManager
from core.requirement_loader import RequirementLoader
from mcp_client import MCPClientManager, SERVERS
from models.state import ExecutionState


DEFAULT_DATASET = str(EVAL_DIR / "eval_dataset_150_cli_version17.csv")
DEFAULT_RAG_FILTERS = [
    {
        "device_type": "router",
        "product": "4000 Series Integrated Services Routers",
        "operating_system": "Cisco IOS XE",
        "version": "17.12.1",
    },
    {
        "device_type": "switch",
        "product": "Catalyst 9300 Series Switches",
        "operating_system": "Cisco IOS XE",
        "version": "17.12.1",
    },
]
ARCH_BASE = {
    "os": "Cisco IOS XE",
    "version": ["17.12.x", "17.12.1", "17.x"],
    "device_type": ["router", "switch"],
    "product": [
        "4000 Series Integrated Services Routers",
        "Catalyst 9300 Series Switches",
    ],
}


class EvaluationResultLogger:
    def save(self, context):
        # The evaluation runner writes one consolidated result file.
        return None


def normalize_config_lines(text):
    lines = []
    for line in str(text or "").splitlines():
        if "#" in line:
            line = line.split("#", 1)[-1]
        elif ">" in line:
            line = line.split(">", 1)[-1]

        line = " ".join(line.split()).lower()
        if line and line not in ("configure terminal", "end", "enable", "no_code"):
            lines.append(line)
    return lines


def ngrams(tokens, n):
    if len(tokens) < n:
        return []
    return [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def f1_score(overlap, pred_total, ref_total):
    if pred_total == 0 or ref_total == 0 or overlap == 0:
        return 0.0
    precision = overlap / pred_total
    recall = overlap / ref_total
    return 2 * precision * recall / (precision + recall)


def rouge_n(prediction, reference, n):
    pred_tokens = " ".join(normalize_config_lines(prediction)).split()
    ref_tokens = " ".join(normalize_config_lines(reference)).split()
    pred_counts = Counter(ngrams(pred_tokens, n))
    ref_counts = Counter(ngrams(ref_tokens, n))
    overlap = sum((pred_counts & ref_counts).values())
    return f1_score(overlap, sum(pred_counts.values()), sum(ref_counts.values()))


def lcs_length(a, b):
    if not a or not b:
        return 0
    previous = [0] * (len(b) + 1)
    for token_a in a:
        current = [0]
        for j, token_b in enumerate(b, start=1):
            if token_a == token_b:
                current.append(previous[j - 1] + 1)
            else:
                current.append(max(previous[j], current[-1]))
        previous = current
    return previous[-1]


def rouge_l(prediction, reference):
    pred_tokens = " ".join(normalize_config_lines(prediction)).split()
    ref_tokens = " ".join(normalize_config_lines(reference)).split()
    overlap = lcs_length(pred_tokens, ref_tokens)
    return f1_score(overlap, len(pred_tokens), len(ref_tokens))


def compute_rouge(predictions, references):
    r1, r2, rl = [], [], []
    for pred, ref in zip(predictions, references):
        if not pred or not ref or str(pred).strip().upper() == "ERROR":
            continue
        r1.append(rouge_n(pred, ref, 1))
        r2.append(rouge_n(pred, ref, 2))
        rl.append(rouge_l(pred, ref))

    def avg(values):
        return round(float(statistics.mean(values)), 4) if values else 0.0

    def std(values):
        return round(float(statistics.pstdev(values)), 4) if values else 0.0

    return {
        "rouge1": avg(r1),
        "rouge1_std": std(r1),
        "rouge2": avg(r2),
        "rouge2_std": std(r2),
        "rougeL": avg(rl),
        "rougeL_std": std(rl),
    }


def read_dataset(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        raise ValueError("Dataset is empty.")

    if "requirement" not in rows[0]:
        raise ValueError("Dataset must contain a 'requirement' column.")

    reference_columns = ["configuration", "confguration", "ground_truth"]
    reference_column = next((col for col in reference_columns if col in rows[0]), None)
    if not reference_column:
        raise ValueError("Dataset must contain 'configuration', 'confguration', or 'ground_truth'.")

    return rows, reference_column


def response_text(response):
    content = response.get("content", [])
    if content and isinstance(content, list):
        text = content[0].get("text", "")
        if isinstance(text, str):
            return text

    data = response.get("data")
    if isinstance(data, str):
        return data

    return ""


def extract_model_outputs(context):
    outputs = []
    skip_prompts = {"VERIFICATION_CALL", "RAG_RETRIEVAL"}
    for iteration in context.iterations:
        if iteration.get("prompt") in skip_prompts:
            continue

        response = iteration.get("response", {})
        if response.get("isError") is False or response.get("status") == "success":
            text = response_text(response)
            if text:
                outputs.append(text)
    return outputs


def verification_statuses(context):
    statuses = []
    for iteration in context.iterations:
        if iteration.get("prompt") == "VERIFICATION_CALL":
            response = iteration.get("response", {})
            statuses.append(response.get("status"))
    return statuses


def build_requirement(row):
    requirement_loader = RequirementLoader()
    requirement = requirement_loader.load(row["requirement"])
    requirement["source"] = "evaluation_dataset"
    requirement["dataset_row_id"] = row.get("id", str(uuid.uuid4()))
    requirement["rag_filters"] = DEFAULT_RAG_FILTERS
    return requirement


def run_evaluation(dataset_path, output_dir, limit=None):
    rows, reference_column = read_dataset(dataset_path)
    if limit:
        rows = rows[:limit]

    templates_path = PROJECT_ROOT / "templates"
    prompt_manager = PromptManager(templates_path=str(templates_path))
    mcp_client_manager = MCPClientManager(SERVERS)
    result_logger = EvaluationResultLogger()

    predictions = []
    references = []
    latencies = []
    csv_rows = []
    states = []
    first_pass_successes = 0
    final_batfish_successes = 0
    refinement_successes = 0
    max_iterations_reached = 0

    try:
        batfish_server = mcp_client_manager.server("batfish")
        batfish_server.start()
        batfish_server.initialize()

        flm_server = mcp_client_manager.server("flm")
        flm_server.start()
        flm_server.initialize()

        controller = ExecutionController(
            prompt_manager=prompt_manager,
            mcp_client=mcp_client_manager,
            result_logger=result_logger,
        )

        for index, row in enumerate(rows, start=1):
            requirement = build_requirement(row)
            reference = row[reference_column]

            print(f"[{index}/{len(rows)}] {requirement['request_id']}")
            start = time.time()
            context = controller.run(requirement)
            elapsed = time.time() - start

            final_prediction = getattr(context, "generated_config", "") or ""
            statuses = verification_statuses(context)
            first_pass_ok = bool(statuses and statuses[0] == "success")
            final_ok = context.state == ExecutionState.SUCCESS
            # Each repair attempt triggers exactly one extra verification call.
            refinement_attempts = max(0, len(statuses) - 1)
            refined_to_success = final_ok and not first_pass_ok and refinement_attempts > 0

            predictions.append(final_prediction)
            references.append(reference)
            latencies.append(elapsed)
            states.append(context.state.value)
            csv_rows.append({
                "requirement": row["requirement"],
                "ground_truth": reference,
                "final_prediction": final_prediction,
            })

            if first_pass_ok:
                first_pass_successes += 1
            if final_ok:
                final_batfish_successes += 1
            if refined_to_success:
                refinement_successes += 1
            if context.state == ExecutionState.MAX_ITERATIONS_REACHED:
                max_iterations_reached += 1

    finally:
        mcp_client_manager.close_all()

    final_rouge = compute_rouge(predictions, references)
    total = len(rows)
    error_count = sum(1 for pred in predictions if str(pred).strip().upper() == "ERROR")
    nocode_count = sum(1 for pred in predictions if str(pred).strip().upper() == "NO_CODE")
    failed_count = sum(1 for state in states if state == ExecutionState.FAILED.value)

    def rate(value):
        return round(value / total, 4) if total else 0.0

    avg_time = statistics.mean(latencies) if latencies else 0.0
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_output_path = output_dir / f"results_NetGen_system_RAG_Batfish_{timestamp}.csv"

    csv_fields = ["requirement", "ground_truth", "final_prediction"]
    with open(csv_output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(csv_rows)

    print("Evaluation completed.")
    print(f"CSV saved to: {csv_output_path}")
    print(f"Total samples: {total}")
    print(f"Total time: {round(sum(latencies), 2)}s")
    print(f"Average time/sample: {round(avg_time, 3)}s")
    print(f"Errors: {error_count}")
    print(f"NO_CODE: {nocode_count}")
    print(f"First-pass Batfish success: {first_pass_successes}/{total} ({rate(first_pass_successes)})")
    print(f"Final Batfish success: {final_batfish_successes}/{total} ({rate(final_batfish_successes)})")
    print(f"Refinement success: {refinement_successes}/{total} ({rate(refinement_successes)})")
    print(f"Max iterations reached: {max_iterations_reached}")
    print(f"Failed: {failed_count}")
    print("Final ROUGE:", final_rouge)


def main():
    parser = argparse.ArgumentParser(description="Evaluate the full NetGen orchestrator system.")
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help=f"Path to evaluation CSV. Default: {DEFAULT_DATASET}",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path("results") / "evaluation"),
        help="Directory where the consolidated evaluation CSV will be saved.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for quick evaluation runs.",
    )
    args = parser.parse_args()

    run_evaluation(
        dataset_path=Path(args.dataset),
        output_dir=Path(args.output_dir),
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
