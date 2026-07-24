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
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core.execution_controller import ExecutionController
from core.prompt_manager import PromptManager
from mcp_client import MCPClientManager, SERVERS
from statistics import mean, median
from utils.create_snapshot import create_snapshot_from_string

from sintaxys.batfish_comparison_summary import (
    BatfishPredictionParser,
    BatfishPQSEvaluator,
    count_expected_files,
)


def print_retry_prompts(context):
    """Imprime en consola los prompts de reintento que el pipeline envio al modelo.

    En context.iterations, la 1a iteracion que NO es VERIFICATION_CALL es la
    generacion inicial; las siguientes no-VERIFICATION son los reintentos
    (correccion de linea o regeneracion completa). Devuelve cuantos reintentos hubo.
    """
    # RAG_RETRIEVAL y VERIFICATION_CALL son marcadores internos, no prompts reales.
    markers = {"RAG_RETRIEVAL", "VERIFICATION_CALL"}
    iters = getattr(context, "iterations", []) or []
    real = [it for it in iters if it.get("prompt") not in markers]
    retries = real[1:]  # real[0] es la generacion inicial; el resto son reintentos
    if retries:
        print(f"REINTENTOS: {len(retries)}")
    for k, it in enumerate(retries, start=1):
        print("." * 78)
        print(f"PROMPT DE REINTENTO #{k} (enviado al modelo):")
        print(it.get("prompt", ""))
        patched = it.get("patched_config")
        if patched:
            print("  -> config reconstruida tras este reintento:")
            print(patched)
        print("." * 78, flush=True)
    return len(retries)


def get_failed_lines(config, parser, mcp):
    """Devuelve las lineas que Batfish rechazo en ESTA config (sin merge, la misma
    que se puntua), via parse_warning. Cada item: {file, line, reason}."""
    parsed = parser.parse_prediction(config)
    if not str(parsed or "").strip():
        return [{"file": "", "line": "", "reason": "config no parseable (UNPARSEABLE)"}]
    tmp = tempfile.mkdtemp(prefix="warn_")
    try:
        zip_path = create_snapshot_from_string(parsed, base_dir=tmp)
        if not zip_path:
            return []
        bf = mcp.server("batfish")
        bf.call_tool("load_snapshot", {"zip_path": zip_path})
        wr = bf.call_tool("parse_warning", {"aggregate_duplicates": True})
        results = []
        if isinstance(wr, dict):
            results = (wr.get("structuredContent", {}) or {}).get("results", []) or []
        out = []
        for w in results:
            text = (w.get("text") or "").strip()
            out.append({
                "file": w.get("filename", ""),
                "line": text or f"Line {w.get('line', '?')}",
                "reason": w.get("comment", "Syntax is unrecognized"),
            })
        return out
    except Exception as exc:
        return [{"file": "", "line": "", "reason": f"parse_warning error: {exc}"}]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def count_retries(context):
    markers = {"RAG_RETRIEVAL", "VERIFICATION_CALL"}
    iters = getattr(context, "iterations", []) or []
    real = [it for it in iters if it.get("prompt") not in markers]
    return max(len(real) - 1, 0)


def batfish_feedback_rounds(context):
    """Retroalimentacion de Batfish que se le mando al modelo: los 'errors' de
    cada VERIFICATION_CALL que fallo, en orden (una ronda por reintento).
    Cada item se compacta a {file, status, line, reason}."""
    rounds = []
    for it in getattr(context, "iterations", []) or []:
        if it.get("prompt") != "VERIFICATION_CALL":
            continue
        resp = it.get("response", {}) or {}
        if resp.get("status") != "failed":
            continue
        data = resp.get("data", {})
        errors = data.get("errors", []) if isinstance(data, dict) else []
        rounds.append([
            {
                "file": e.get("File", ""),
                "status": e.get("Status", ""),
                "line": e.get("Invalid line", ""),
                "reason": e.get("Reason", ""),
            }
            for e in errors if isinstance(e, dict)
        ])
    return rounds


def config_after_retry(it):
    """Config resultante de un reintento: la reconstruida (patched_config) en el
    modo quirurgico, o la respuesta del modelo en la regeneracion completa."""
    patched = it.get("patched_config")
    if patched:
        return patched
    resp = it.get("response", {}) or {}
    content = resp.get("content", [])
    if content and isinstance(content, list) and content:
        t = content[0].get("text", "")
        if isinstance(t, str):
            return t.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
    return ""


def retry_timeline(context):
    """Eventos en orden de ejecucion:
       ('feedback', errors[])  -> lo que Batfish rechazo y se le mando al modelo
       ('corrected', config)   -> la config corregida que devolvio el modelo
    """
    events = []
    gen_seen = False
    for it in getattr(context, "iterations", []) or []:
        p = it.get("prompt")
        if p == "RAG_RETRIEVAL":
            continue
        if p == "VERIFICATION_CALL":
            resp = it.get("response", {}) or {}
            if resp.get("status") == "failed":
                data = resp.get("data", {})
                raw = data.get("errors", []) if isinstance(data, dict) else []
                events.append(("feedback", [
                    {
                        "file": e.get("File", ""),
                        "status": e.get("Status", ""),
                        "line": e.get("Invalid line", ""),
                        "reason": e.get("Reason", ""),
                    }
                    for e in raw if isinstance(e, dict)
                ]))
            continue
        # prompt real: la primera es la generacion inicial; el resto, reintentos
        if not gen_seen:
            gen_seen = True
            events.append(("initial", config_after_retry(it)))
            continue
        events.append(("corrected", config_after_retry(it)))
    return events


DATASET = BASE / "eval_dataset_150_cli_version17.csv"

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
BATFISH_FEEDBACK_JSON = OUT_DIR / "batfish_feedback.json"
PARSER_DISCARDS_JSON = OUT_DIR / "parser_discards.json"
SELECTED_RESULTS_JSON = OUT_DIR / "selected_results.json"

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
                if not str(rec.get("configuration", "") or "").strip():
                    continue
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


def load_records_json(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        corrupt_path = path.with_suffix(path.suffix + f".corrupt.{int(time.time())}")
        path.replace(corrupt_path)
        print(f"AVISO: {path} no es JSON valido; se conservo en {corrupt_path}", flush=True)
        return {}
    records = data.get("records", data if isinstance(data, list) else [])
    return {
        int(record["index"]): record
        for record in records
        if isinstance(record, dict) and "index" in record
    }


def write_records_json(path, records):
    ordered = [records[i] for i in sorted(records)]
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps({"records": ordered}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def parse_indices(value):
    if not value:
        return []
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def set_output_dir(path):
    global OUT_DIR, RESULTS_JSONL, PREDICTIONS_JSON
    global BATFISH_FEEDBACK_JSON, PARSER_DISCARDS_JSON, SELECTED_RESULTS_JSON

    OUT_DIR = path
    RESULTS_JSONL = OUT_DIR / "results.jsonl"
    PREDICTIONS_JSON = OUT_DIR / "predictions.json"
    BATFISH_FEEDBACK_JSON = OUT_DIR / "batfish_feedback.json"
    PARSER_DISCARDS_JSON = OUT_DIR / "parser_discards.json"
    SELECTED_RESULTS_JSON = OUT_DIR / "selected_results.json"


def load_batfish_feedback():
    return load_records_json(BATFISH_FEEDBACK_JSON)


def write_batfish_feedback(records):
    write_records_json(BATFISH_FEEDBACK_JSON, records)


def verification_feedback_payloads(context):
    """Feedback compacto ya generado por el sistema para enviarlo al modelo."""
    payloads = []
    for it in getattr(context, "iterations", []) or []:
        if it.get("prompt") != "VERIFICATION_CALL":
            continue
        resp = it.get("response", {}) or {}
        if resp.get("status") != "failed":
            continue
        payloads.append(resp.get("data", {}))
    return payloads


def load_parser_discards():
    return load_records_json(PARSER_DISCARDS_JSON)


def write_parser_discards(records):
    write_records_json(PARSER_DISCARDS_JSON, records)


def parser_discard_reason(config, parser):
    parsed = parser.parse_prediction(config)
    parsed_text = str(parsed or "").strip()
    if parsed_text and parsed_text.lower() != "none":
        return None

    text = str(config or "").strip()
    if not text:
        return "empty_configuration"
    if "<" in text and ">" in text:
        return "contains_placeholder_or_angle_bracket_token"

    from utils.batfish_parser import PROMPT_RE, BatfishPredictionParser as ParserRules

    prompt_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = PROMPT_RE.match(line)
        if match:
            prompt_lines.append((match.group(2) or "", match.group(3).strip()))

    if not prompt_lines:
        return "no_cisco_cli_prompt_lines_matched"

    non_skipped = [cmd for _, cmd in prompt_lines if cmd and not ParserRules._is_skip(cmd)]
    if not non_skipped:
        return "only_operational_or_non_persistent_commands"

    has_block_opener = any(ParserRules._is_block_opener(cmd) for cmd in non_skipped)
    has_submode_command = any((mode or "").strip().lower() not in ("", "config") for mode, cmd in prompt_lines if cmd)
    if has_submode_command and not has_block_opener:
        return "submode_commands_without_parent_mode_command"

    return "parser_returned_empty"


def parser_discard_records(initial_config, retry_configs, final_config, parser):
    records = []
    attempts = []
    if str(initial_config or "").strip():
        attempts.append(("initial", initial_config))
    for idx, cfg in enumerate(retry_configs, start=1):
        if str(cfg or "").strip():
            attempts.append((f"retry_{idx}", cfg))
    if str(final_config or "").strip():
        attempts.append(("final", final_config))

    seen = set()
    for stage, cfg in attempts:
        key = (stage, str(cfg))
        if key in seen:
            continue
        seen.add(key)
        reason = parser_discard_reason(cfg, parser)
        if reason:
            records.append({
                "stage": stage,
                "reason": reason,
                "configuration": cfg,
            })
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Cuantos evaluar (0 = todos)")
    ap.add_argument("--start", type=int, default=0, help="Indice inicial (0-based)")
    ap.add_argument("--indices", default="",
                    help="Indices especificos 0-based separados por coma, ej: 20,105,108")
    ap.add_argument("--out-suffix", default="",
                    help="Sufijo para guardar en results/v17_eval_<sufijo>")
    ap.add_argument("--verbose", action="store_true",
                    help="Muestra tambien config final, prompts de reintento y lineas (score-consistentes)")
    args = ap.parse_args()

    selected_indices = parse_indices(args.indices)
    if args.out_suffix:
        set_output_dir(BASE / "results" / f"v17_eval_{args.out_suffix}")
    elif selected_indices:
        set_output_dir(BASE / "results" / "v17_eval_selected")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_rows()
    done = load_done()
    batfish_feedback_records = load_batfish_feedback()
    parser_discard_records_by_index = load_parser_discards()

    if selected_indices:
        todo = [i for i in selected_indices if 0 <= i < len(rows) and i not in done]
    else:
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

    # Acumuladores estilo REMOTO (origin/main summarize_model): conteo POR ARCHIVO
    # + PQS promedio/mediana. Se reconstruyen de lo ya guardado para poder reanudar.
    agg = {
        "total_modified_files": 0,
        "total_passed": 0,
        "total_partially_parsed": 0,
        "total_failed": 0,
        "total_unknown": 0,
    }
    pqs_values = []
    for k in sorted(done):
        rec = done[k]
        agg["total_modified_files"] += rec.get("files", 0)
        agg["total_passed"] += rec.get("passed", 0)
        agg["total_partially_parsed"] += rec.get("partially_parsed", 0)
        agg["total_failed"] += rec.get("failed", 0)
        agg["total_unknown"] += rec.get("unknown", 0)
        pqs_values.append(rec.get("pqs", 0.0))

    try:
        for n, i in enumerate(todo, start=1):
            row = rows[i]
            requirement_text = row["requirement"]

            print(SEP)
            print(f"[{n}/{len(todo)}] #{i}  {requirement_text[:100]}", flush=True)

            requirement = {
                "request_id": str(uuid.uuid4()),
                "intent": requirement_text,
                "rag_filters": RAG_FILTERS,
            }

            t0 = time.time()
            context = None
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

            events = retry_timeline(context) if context is not None else []
            initial_config = next((v for k, v in events if k == "initial"), "")
            rounds = [v for k, v in events if k == "feedback"]
            retry_configs = [v for k, v in events if k == "corrected"]
            n_retries = len(retry_configs)

            # --- Ciclo completo en consola: config inicial -> feedback de Batfish
            #     al modelo -> config corregida, por cada reintento ---
            if events:
                for kind, val in events:
                    if kind == "initial":
                        print("  config inicial generada (la que se manda a Batfish):")
                        print(val if str(val).strip() else "    (vacia)")
                    elif kind == "feedback":
                        print("  feedback Batfish -> modelo:")
                        if not val:
                            print("    (sin detalle de lineas)")
                        for e in val:
                            loc = f"[{e['file']}] " if e.get("file") else ""
                            print(f"    - {loc}{e['line']}")
                            print(f"        motivo: {e['reason']}")
                    else:  # corrected
                        print("  config corregida por el modelo:")
                        print(val if str(val).strip() else "    (vacia)")
            else:
                print("  (sin iteraciones registradas)")

            # --- Puntuacion Batfish: MISMA agregacion que el summary del remoto ---
            parsed_config = parser.parse_prediction(config)
            result = evaluator.verify_prediction(parsed_config)
            counts = result.parse_status_counts

            expected_files = count_expected_files(config, parser)
            parser_unknown_files = max(expected_files - result.total_modified_files, 0)
            accounted_files = result.total_modified_files + parser_unknown_files
            score_sum = result.pqs_modified * result.total_modified_files
            pqs = score_sum / accounted_files if accounted_files else 0.0

            q_passed = counts.get("PASSED", 0)
            q_partial = counts.get("PARTIALLY_PARSED", 0)
            q_failed = counts.get("FAILED", 0)
            q_unknown = counts.get("UNKNOWN", 0) + parser_unknown_files

            agg["total_modified_files"] += accounted_files
            agg["total_passed"] += q_passed
            agg["total_partially_parsed"] += q_partial
            agg["total_failed"] += q_failed
            agg["total_unknown"] += q_unknown
            pqs_values.append(pqs)

            file_statuses = "; ".join(
                f"{f['file_name']}={f['status']}" for f in result.files
            )

            # Lineas score-consistentes (unmerged) solo se guardan/muestran en verbose.
            failed_lines = []
            if args.verbose and (q_failed or q_partial or q_unknown):
                failed_lines = get_failed_lines(config, parser, mcp)

            print(
                f"  resultado: passed={q_passed} partial={q_partial} "
                f"failed={q_failed} unknown={q_unknown}  pqs={pqs:.4f}  "
                f"reintentos={n_retries}  [{elapsed:.1f}s]",
                flush=True,
            )

            # Guardado incremental (reanudable) — el detalle completo va al archivo.
            rec = {
                "index": i,
                "requirement": requirement_text,
                "configuration": config,
                "passed": q_passed,
                "partially_parsed": q_partial,
                "failed": q_failed,
                "unknown": q_unknown,
                "files": accounted_files,
                "pqs": round(pqs, 6),
                "file_statuses": file_statuses,
                "initial_config": initial_config,
                "batfish_feedback": rounds,
                "retry_configs": retry_configs,
                "failed_lines": failed_lines,
                "retries": n_retries,
                "note": result.note,
                "seconds": round(elapsed, 1),
            }
            with open(RESULTS_JSONL, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            done[i] = rec
            write_predictions(done)
            if selected_indices:
                write_records_json(
                    SELECTED_RESULTS_JSON,
                    {idx: done[idx] for idx in selected_indices if idx in done},
                )

            batfish_feedback_records[i] = {
                "index": i,
                "requirement": requirement_text,
                "feedback_rounds": verification_feedback_payloads(context) if context is not None else [],
                "initial_config": initial_config,
                "retry_configs": retry_configs,
                "final_configuration": config,
                "final_score": {
                    "passed": q_passed,
                    "partially_parsed": q_partial,
                    "failed": q_failed,
                    "unknown": q_unknown,
                    "files": accounted_files,
                    "pqs": round(pqs, 6),
                    "file_statuses": file_statuses,
                    "note": result.note,
                },
            }
            write_batfish_feedback(batfish_feedback_records)

            discards = parser_discard_records(initial_config, retry_configs, config, parser)
            if discards:
                parser_discard_records_by_index[i] = {
                    "index": i,
                    "requirement": requirement_text,
                    "discards": discards,
                }
            else:
                parser_discard_records_by_index.pop(i, None)
            write_parser_discards(parser_discard_records_by_index)

    finally:
        print("\nCerrando MCP servers...", flush=True)
        mcp.close_all()

    # --- Resumen final: MISMOS campos que summarize_model del remoto ---
    n_rows = len(done)
    avg_pqs = mean(pqs_values) if pqs_values else 0.0
    med_pqs = median(pqs_values) if pqs_values else 0.0
    print("\n" + SEP)
    print("RESUMEN FINAL  (metrica IDENTICA al summary del remoto: por archivo/dispositivo)")
    print(SEP)
    print(f"  rows (preguntas)       : {n_rows}")
    print(f"  avg_pqs_modified       : {avg_pqs:.4f}")
    print(f"  median_pqs_modified    : {med_pqs:.4f}")
    print(f"  total_modified_files   : {agg['total_modified_files']}")
    print(f"  total_passed           : {agg['total_passed']}")
    print(f"  total_partially_parsed : {agg['total_partially_parsed']}")
    print(f"  total_failed           : {agg['total_failed']}")
    print(f"  total_unknown          : {agg['total_unknown']}")
    print(SEP)
    print(f"predictions.json: {PREDICTIONS_JSON}")
    print(f"detalle (jsonl) : {RESULTS_JSONL}")
    print(f"batfish feedback: {BATFISH_FEEDBACK_JSON}")
    print(f"parser discards : {PARSER_DISCARDS_JSON}")
    if selected_indices:
        print(f"selected results: {SELECTED_RESULTS_JSON}")


if __name__ == "__main__":
    main()
