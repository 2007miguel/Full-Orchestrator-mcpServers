"""
Runner de evaluacion v17 - EN VIVO, uno por uno.

Por cada requerimiento del dataset:
  1. Corre el pipeline completo del Orchestrator (normaliza -> RAG -> genera -> verifica -> reintentos).
  2. Imprime el requerimiento y la configuracion generada.
  3. La puntua con Batfish y muestra el veredicto (PASSED / PARTIALLY_PARSED / FAILED / UNKNOWN).
  4. Acumula y muestra los totales corrientes.

Al final imprime el resumen: correct (PASSED), partially_correct (PARTIALLY_PARSED),
error (FAILED + UNKNOWN), y guarda predictions.json para el scorer oficial.

Requiere: server de Colab (ngrok) ARRIBA + Docker con el contenedor batfish (9996/9997).
Es reanudable: si se corta, vuelve a correrlo y salta los ya hechos.
"""
import argparse
import csv
import json
import sys
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core.execution_controller import ExecutionController
from core.prompt_manager import PromptManager
from mcp_client import MCPClientManager, SERVERS
from sintaxys.batfish_comparison_summary import (
    BatfishPredictionParser,
    BatfishPQSEvaluator,
    verdict_for,
    count_expected_files,
)

DATASET = (
    "/Users/cristianfelipebolanosortega/Library/CloudStorage/"
    "GoogleDrive-felproposalchat@gmail.com/Mi unidad/base + rag v 17/"
    "dataset evaluacion v17/eval_dataset_150_cli_version17.csv"
)

# arch_base identico a v17: router ISR4000 + switch Catalyst 9300, IOS XE, 17.12.1
# (17.12.1 se expande a {17.12.1, 17.12.x, 17.x}, el mismo set que ARCH_BASE de v17).
RAG_FILTERS = [
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

OUT_DIR = BASE / "results" / "v17_eval"
RESULTS_JSONL = OUT_DIR / "results.jsonl"
PREDICTIONS_JSON = OUT_DIR / "predictions.json"

SEP = "=" * 78


class NullLogger:
    def save(self, context):
        pass


def load_rows():
    with open(DATASET, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_done():
    """Indices ya evaluados (para reanudar), leidos del JSONL."""
    done = {}
    if RESULTS_JSONL.exists():
        for line in RESULTS_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done[int(rec["index"])] = rec
            except Exception:
                pass
    return done


def write_predictions(done):
    """predictions.json en el formato del scorer oficial, en orden de indice."""
    preds = [done[i]["configuration"] for i in sorted(done)]
    PREDICTIONS_JSON.write_text(
        json.dumps({"predictions": preds}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Cuantos evaluar (0 = todos)")
    ap.add_argument("--start", type=int, default=0, help="Indice inicial (0-based)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_rows()
    done = load_done()

    end = len(rows) if args.limit <= 0 else min(args.start + args.limit, len(rows))
    todo = [i for i in range(args.start, end) if i not in done]

    print(SEP)
    print(f"EVAL v17  |  dataset: {len(rows)} reqs  |  ya hechos: {len(done)}  |  por hacer ahora: {len(todo)}")
    print(f"Salida: {OUT_DIR}")
    print(SEP, flush=True)

    prompt_manager = PromptManager(templates_path=str(BASE / "templates"))
    mcp = MCPClientManager(SERVERS)

    print("Arrancando MCP servers (batfish + flm)...", flush=True)
    for name in ("batfish", "flm"):
        srv = mcp.server(name)
        srv.start()
        srv.initialize()
    print("MCP listos.\n", flush=True)

    controller = ExecutionController(
        prompt_manager=prompt_manager,
        mcp_client=mcp,
        result_logger=NullLogger(),
    )
    parser = BatfishPredictionParser()
    evaluator = BatfishPQSEvaluator(mcp)

    tally = {"PASSED": 0, "PARTIALLY_PARSED": 0, "FAILED": 0, "UNKNOWN": 0}
    for rec in done.values():
        v = rec.get("verdict")
        if v in tally:
            tally[v] += 1

    try:
        for n, i in enumerate(todo, start=1):
            row = rows[i]
            requirement_text = row["requirement"]

            print(SEP)
            print(f"[{n}/{len(todo)}]  (fila #{i})  REQUERIMIENTO:")
            print(f"  {requirement_text}")
            print("-" * 78, flush=True)

            requirement = {
                "request_id": str(uuid.uuid4()),
                "intent": requirement_text,
                "rag_filters": RAG_FILTERS,
            }

            t0 = time.time()
            try:
                context = controller.run(requirement)
                config = (
                    getattr(context, "generated_config", "")
                    or getattr(context, "final_result", "")
                    or ""
                )
            except Exception as exc:
                config = ""
                print(f"  [ERROR en generacion]: {exc}", flush=True)
            elapsed = time.time() - t0

            print("CONFIG GENERADA:")
            print(config if config.strip() else "  (vacia)")
            print("-" * 78, flush=True)

            # --- Puntuacion Batfish (misma logica que el scorer oficial) ---
            parsed_config = parser.parse_prediction(config)
            result = evaluator.verify_prediction(parsed_config)
            expected_files = count_expected_files(config, parser)
            unknown_files = max(expected_files - result.total_modified_files, 0)
            verdict = verdict_for(result.parse_status_counts, unknown_files)
            tally[verdict] = tally.get(verdict, 0) + 1

            note = f"  ({result.note})" if result.note else ""
            print(f"VEREDICTO: {verdict}{note}   [{elapsed:.1f}s]")
            correct = tally["PASSED"]
            partial = tally["PARTIALLY_PARSED"]
            error = tally["FAILED"] + tally["UNKNOWN"]
            total = correct + partial + error
            print(
                f"TOTALES: correct={correct}  partially_correct={partial}  "
                f"error={error}  (de {total})",
                flush=True,
            )

            # Guardado incremental (reanudable)
            with open(RESULTS_JSONL, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "index": i,
                    "requirement": requirement_text,
                    "configuration": config,
                    "verdict": verdict,
                    "note": result.note,
                    "seconds": round(elapsed, 1),
                }, ensure_ascii=False) + "\n")
            done[i] = {"index": i, "configuration": config, "verdict": verdict}
            write_predictions(done)

    finally:
        print("\nCerrando MCP servers...", flush=True)
        mcp.close_all()

    # --- Resumen final ---
    correct = tally["PASSED"]
    partial = tally["PARTIALLY_PARSED"]
    error = tally["FAILED"] + tally["UNKNOWN"]
    total = correct + partial + error
    print("\n" + SEP)
    print("RESUMEN FINAL (v17)")
    print(SEP)
    print(f"  Total evaluados      : {total}")
    def pct(x):
        return f"{100.0 * x / total:.1f}%" if total else "0%"
    print(f"  correct (PASSED)             : {correct:>3}  {pct(correct)}")
    print(f"  partially_correct (PARTIAL)  : {partial:>3}  {pct(partial)}")
    print(f"  error (FAILED + UNKNOWN)     : {error:>3}  {pct(error)}")
    print(f"      - FAILED : {tally['FAILED']}")
    print(f"      - UNKNOWN: {tally['UNKNOWN']}")
    print(SEP)
    print(f"predictions.json: {PREDICTIONS_JSON}")
    print(f"detalle (jsonl) : {RESULTS_JSONL}")


if __name__ == "__main__":
    main()
