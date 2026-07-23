import json
import statistics
from pathlib import Path

json_path = Path("base+RAG_nuevodataset/base+RAG_nuevodataset.json")

data = json.loads(json_path.read_text(encoding="utf-8"))

top_scores = []

# Formato RAG: top_score está dentro de retrieved_logs
if isinstance(data.get("retrieved_logs"), list):
    for item in data["retrieved_logs"]:
        if isinstance(item, dict) and isinstance(item.get("top_score"), (int, float)):
            top_scores.append(item["top_score"])

# Formato con predictions en la raíz
elif isinstance(data.get("predictions"), list):
    for item in data["predictions"]:
        if isinstance(item, dict) and isinstance(item.get("top_score"), (int, float)):
            top_scores.append(item["top_score"])

# Formato multi-modelo: results[] con predictions[] o retrieved_logs[]
elif isinstance(data.get("results"), list):
    for result in data["results"]:
        for key in ("retrieved_logs", "predictions"):
            for item in result.get(key, []):
                if isinstance(item, dict) and isinstance(item.get("top_score"), (int, float)):
                    top_scores.append(item["top_score"])

if not top_scores:
    raise ValueError("No se encontraron valores top_score")

print(f"Cantidad: {len(top_scores)}")
print(f"Media: {statistics.mean(top_scores):.4f}")
print(f"Mediana: {statistics.median(top_scores):.4f}")
print(f"Mínimo: {min(top_scores):.4f}")
print(f"Máximo: {max(top_scores):.4f}")

if len(top_scores) > 1:
    print(f"Desviación estándar: {statistics.stdev(top_scores):.4f}")
    print(f"Varianza: {statistics.variance(top_scores):.4f}")

scores_sorted = sorted(top_scores)


def percentile(values, p):
    index = round((len(values) - 1) * p)
    return values[index]


print(f"P25: {percentile(scores_sorted, 0.25):.4f}")
print(f"P75: {percentile(scores_sorted, 0.75):.4f}")
