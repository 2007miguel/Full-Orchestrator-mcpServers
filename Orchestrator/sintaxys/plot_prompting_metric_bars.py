#!/usr/bin/env python3

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent

# Configure the techniques and JSON sources here.
TECHNIQUE_FILES = {
    "Zero-shot": SCRIPT_DIR / "base" / "generation_results_sin_plan_zeroshot_20260717_073931.json",
    "CoT": SCRIPT_DIR / "base+pasos" / "generation_results_con_plan_zeroshot_20260717_165238.json",
    "Few-shot": SCRIPT_DIR / "base+fewshot" / "generation_results_sin_plan_fewshot_20260717_104316.json",
    "CoT + Few-shot": SCRIPT_DIR / "fewshot+pasos" / "generation_results_con_plan_fewshot_20260717_204239.json",
}

# Set MODELS to a custom list to control order, or keep None to use the first JSON order.
MODELS = None

# Configure the metrics and output images here.
METRICS = {
    "BERTScore F1": {
        "json_key": "bertscore_f1",
        "output_file": SCRIPT_DIR / "prompting_bertscore_f1_by_model.png",
        "ylim": (0.78, 1.005),
    },
    "ROUGE-L": {
        "json_key": "rougeL",
        "output_file": SCRIPT_DIR / "prompting_rouge_l_by_model.png",
        "ylim": None,
    },
}

MODEL_COLORS = {
    "Llama-3.1-8B-Instruct": "#4C78A8",
    "Zephyr-7B": "#F58518",
    "Qwen2.5-7B-Instruct": "#54A24B",
    "Gemma-2-9B-it": "#72B7B2",
    "FLAN-T5-large": "#E377C2",
    "FLAN-T5-base": "#B279A2",
}


def load_metric_values(json_path: Path, metric_key: str) -> dict[str, float]:
    """Read one metric value per model from a result JSON file."""
    with json_path.open(encoding="utf-8") as file:
        payload = json.load(file)

    values = {}
    for result in payload["results"]:
        model_name = result["model_name"]
        if model_name in values:
            raise ValueError(f"Repeated model {model_name!r} in {json_path}")
        if metric_key not in result:
            raise ValueError(f"Missing metric {metric_key!r} for {model_name!r} in {json_path}")
        values[model_name] = float(result[metric_key])

    return values


def get_models() -> list[str]:
    """Use the configured model order or infer it from the first technique JSON."""
    if MODELS is not None:
        return list(MODELS)

    first_path = next(iter(TECHNIQUE_FILES.values()))
    with first_path.open(encoding="utf-8") as file:
        payload = json.load(file)
    return [result["model_name"] for result in payload["results"]]


def collect_metric_data(metric_key: str, models: list[str]) -> dict[str, list[float]]:
    """Validate and collect exactly one value per model and technique."""
    data = {model_name: [] for model_name in models}

    for technique, json_path in TECHNIQUE_FILES.items():
        values = load_metric_values(json_path, metric_key)
        missing_models = set(models) - set(values)
        extra_models = set(values) - set(models)
        if missing_models or extra_models:
            raise ValueError(
                f"Model mismatch for {technique}: "
                f"missing={sorted(missing_models)}, extra={sorted(extra_models)}"
            )

        for model_name in models:
            data[model_name].append(values[model_name])

    expected_count = len(TECHNIQUE_FILES)
    for model_name, values in data.items():
        if len(values) != expected_count:
            raise ValueError(
                f"{model_name!r} has {len(values)} values; expected {expected_count}"
            )

    return data


def calculate_ylim(values: list[float], configured_ylim: tuple[float, float] | None) -> tuple[float, float]:
    """Use a tight vertical range to emphasize small metric differences."""
    if configured_ylim is not None:
        return configured_ylim

    value_min = min(values)
    value_max = max(values)
    padding = max((value_max - value_min) * 0.12, 0.015)
    return max(0.0, value_min - padding), min(1.005, value_max + padding)


def plot_grouped_bars(metric_name: str, metric_config: dict[str, object], models: list[str]) -> None:
    """Create and save one grouped bar chart for the selected metric."""
    data = collect_metric_data(str(metric_config["json_key"]), models)
    techniques = list(TECHNIQUE_FILES.keys())
    x_positions = np.arange(len(techniques))
    bar_width = min(0.8 / len(models), 0.13)

    plt.style.use("default")
    figure, axis = plt.subplots(figsize=(12, 8), facecolor="white")
    axis.set_facecolor("white")

    for model_index, model_name in enumerate(models):
        offset = (model_index - (len(models) - 1) / 2) * bar_width
        values = data[model_name]
        bars = axis.bar(
            x_positions + offset,
            values,
            width=bar_width,
            label=model_name,
            color=MODEL_COLORS.get(model_name),
            edgecolor="black",
            linewidth=0.5,
        )

        # Add compact value labels above each bar.
        for bar, value in zip(bars, values):
            axis.annotate(
                f"{value:.3f}",
                xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=7,
                fontweight="bold",
            )

    all_values = [value for model_values in data.values() for value in model_values]
    axis.set_ylim(calculate_ylim(all_values, metric_config["ylim"]))
    axis.set_title(
        f"{metric_name} by Prompting Technique and Model",
        fontweight="bold",
        loc="center",
        pad=18,
    )
    axis.set_xlabel("Prompting Technique")
    axis.set_ylabel(metric_name)
    axis.set_xticks(x_positions)
    axis.set_xticklabels(techniques, rotation=0)
    axis.grid(axis="y", linestyle="--", color="lightgray", alpha=0.7)
    axis.set_axisbelow(True)
    axis.legend(title="Model", loc="upper left", bbox_to_anchor=(0.0, 1.30), frameon=True)

    figure.tight_layout()
    figure.savefig(metric_config["output_file"], dpi=300, bbox_inches="tight")
    plt.close(figure)
    print(f"Saved: {metric_config['output_file']}")


def main() -> None:
    models = get_models()
    for metric_name, metric_config in METRICS.items():
        plot_grouped_bars(metric_name, metric_config, models)


if __name__ == "__main__":
    main()
