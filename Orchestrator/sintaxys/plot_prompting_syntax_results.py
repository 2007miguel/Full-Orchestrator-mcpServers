#!/usr/bin/env python3

import csv
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import colors as mcolors
from matplotlib.patches import Patch


SCRIPT_DIR = Path(__file__).resolve().parent

# Configure the techniques and CSV sources here.
TECHNIQUE_FILES = {
    "Zero-shot": SCRIPT_DIR / "base" / "base.csv",
    "CoT": SCRIPT_DIR / "base+pasos" / "base+pasos.csv",
    "Few-shot": SCRIPT_DIR / "base+fewshot" / "base+fewshot.csv",
    "CoT + Few-shot": SCRIPT_DIR / "fewshot+pasos" / "fewshot+pasos.csv",
}

# Set MODELS to a custom list to control order, or keep None to use the first CSV order.
MODELS = None
OUTPUT_FILE = SCRIPT_DIR / "prompting_syntax_results.png"

STATUS_COLUMNS = {
    "Correct": "model",
    "Partially correct": "light_model",
    "Error / Failed / Unknown": "#E45756",
}
MODEL_COLORS = {
    "Llama-3.1-8B-Instruct": "#4C78A8",
    "Zephyr-7B": "#F58518",
    "Qwen2.5-7B-Instruct": "#54A24B",
    "Gemma-2-9B-it": "#72B7B2",
    "FLAN-T5-large": "#E377C2",
    "FLAN-T5-base": "#B279A2",
}
STATUS_LEGEND_COLORS = {
    "Correct": "#555555",
    "Partially correct": "#A6A6A6",
    "Error / Failed / Unknown": "#E45756",
}


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    """Read all model rows from a syntax summary CSV file."""
    with csv_path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def get_models() -> list[str]:
    """Use the configured model order or infer it from the first CSV."""
    if MODELS is not None:
        return list(MODELS)

    first_path = next(iter(TECHNIQUE_FILES.values()))
    return [row["model_name"] for row in load_rows(first_path)]


def collect_percentages(models: list[str]) -> dict[str, dict[str, dict[str, float]]]:
    """Validate one row per model and calculate stacked percentages."""
    data = {}

    for technique, csv_path in TECHNIQUE_FILES.items():
        rows = load_rows(csv_path)
        rows_by_model = {}
        for row in rows:
            model_name = row["model_name"]
            if model_name in rows_by_model:
                raise ValueError(f"Repeated model {model_name!r} in {csv_path}")
            rows_by_model[model_name] = row

        missing_models = set(models) - set(rows_by_model)
        extra_models = set(rows_by_model) - set(models)
        if missing_models or extra_models:
            raise ValueError(
                f"Model mismatch for {technique}: "
                f"missing={sorted(missing_models)}, extra={sorted(extra_models)}"
            )

        data[technique] = {}
        for model_name in models:
            row = rows_by_model[model_name]
            counts = {
                "Correct": int(row["total_passed"]),
                "Partially correct": int(row["total_partially_parsed"]),
                "Error / Failed / Unknown": int(row["total_failed"]) + int(row["total_unknown"]),
            }
            total = sum(counts.values())
            if total == 0:
                raise ValueError(f"{model_name!r} in {technique} has zero syntax classifications")

            data[technique][model_name] = {
                status: count / total * 100.0
                for status, count in counts.items()
            }

    return data


def lighten_color(color: str, amount: float = 0.45) -> tuple[float, float, float]:
    """Return a lighter version of a model color for partially correct syntax."""
    red, green, blue = mcolors.to_rgb(color)
    return (
        red + (1.0 - red) * amount,
        green + (1.0 - green) * amount,
        blue + (1.0 - blue) * amount,
    )


def status_color(status: str, model_name: str) -> str | tuple[float, float, float]:
    """Map each syntax status to the model color, a lighter model color, or red."""
    model_color = MODEL_COLORS[model_name]
    if STATUS_COLUMNS[status] == "model":
        return model_color
    if STATUS_COLUMNS[status] == "light_model":
        return lighten_color(model_color)
    return STATUS_COLUMNS[status]


def plot_syntax_percentages() -> None:
    """Create and save the grouped 100 percent stacked syntax chart."""
    models = get_models()
    data = collect_percentages(models)
    techniques = list(TECHNIQUE_FILES.keys())
    bar_width = min(0.8 / len(models), 0.13)

    figure, axis = plt.subplots(figsize=(14, 8), facecolor="white")
    axis.set_facecolor("white")

    for technique_index, technique in enumerate(techniques):
        for model_index, model_name in enumerate(models):
            x_position = technique_index + (model_index - (len(models) - 1) / 2) * bar_width
            bottom = 0.0

            # Stack each syntax status so every bar reaches 100 percent.
            for status in STATUS_COLUMNS:
                percentage = data[technique][model_name][status]
                axis.bar(
                    x_position,
                    percentage,
                    width=bar_width,
                    bottom=bottom,
                    color=status_color(status, model_name),
                    edgecolor="black",
                    linewidth=0.5,
                )

                if percentage >= 5:
                    axis.text(
                        x_position,
                        bottom + percentage / 2,
                        f"{percentage:.1f}%",
                        ha="center",
                        va="center",
                        rotation=90,
                        fontsize=7,
                        fontweight="bold",
                        color="white" if status != "Partially correct" else "black",
                    )
                bottom += percentage

    axis.set_title(
        "Syntax Classification by Prompting Technique and Model",
        fontweight="bold",
        loc="center",
        pad=18,
    )
    axis.set_xlabel("Prompting Technique")
    axis.set_ylabel("Percentage (%)")
    axis.set_xticks(range(len(techniques)))
    axis.set_xticklabels(techniques, rotation=0)
    axis.set_ylim(0, 100)
    axis.grid(axis="y", linestyle="--", color="lightgray", alpha=0.7)
    axis.set_axisbelow(True)

    status_legend = axis.legend(
        handles=[
            Patch(facecolor=color, edgecolor="black", label=status)
            for status, color in STATUS_LEGEND_COLORS.items()
        ],
        title="Syntax status",
        loc="upper left",
        bbox_to_anchor=(0.0, 1.35),
        ncol=3,
        frameon=True,
    )
    axis.add_artist(status_legend)

    axis.legend(
        handles=[
            Patch(
                facecolor=MODEL_COLORS[model_name],
                edgecolor="black",
                label=model_name,
            )
            for model_name in models
        ],
        title="Model",
        loc="upper right",
        bbox_to_anchor=(1.0, 1.35),
        ncol=2,
        frameon=True,
    )

    figure.tight_layout()
    figure.savefig(OUTPUT_FILE, dpi=300, bbox_inches="tight")
    plt.close(figure)
    print(f"Saved: {OUTPUT_FILE}")


if __name__ == "__main__":
    plot_syntax_percentages()
