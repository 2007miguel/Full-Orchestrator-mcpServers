# core/execution_controller.py

import json
from models.execution_context import ExecutionContext
from models.state import ExecutionState

# Token budget for intent normalization, matching v17 (MAX_NEW_TOKENS_INTENT).
# The requirement -> retrieval-query rewrite is a short technical line.
MAX_NEW_TOKENS_INTENT = 120


class ExecutionController:
    """
    Orchestrates the end-to-end process of converting a natural language requirement
    into a verified network configuration.
    """

    def __init__(
        self,
        prompt_manager,
        mcp_client,
        result_logger,
        max_refinement_iterations=3
    ):
        """
        Initializes the controller with its dependencies.

        Args:
            prompt_manager: An object to build prompts for the language model.
            mcp_client: A client to interact with external tools (like FLM or verifier).
            result_logger: An object to save the final execution context.
            max_refinement_iterations (int): Max attempts for the repair loop.
        """
        self.prompt_manager = prompt_manager
        self.mcp_client = mcp_client
        self.result_logger = result_logger
        self.max_refinement_iterations = max_refinement_iterations

    def run(self, requirement: dict) -> ExecutionContext:
        """
        Executes the orchestration pipeline:
        1. Intent normalization
        2. RAG retrieval
        3. Configuration generation
        4. Verification
        5. Refinement loop (if needed)
        """
        import time
        start_time = time.time()
        
        # Create a unique context to track this specific execution's state and history.
        context = ExecutionContext(requirement)

        try:
            # Inicializar CSV a través de mcp-server-csv
            try:
                csv_server = self.mcp_client.server("csv")
                if not csv_server._initialized:
                    csv_server.start()
                    csv_server.initialize()
                csv_server.call_tool("create_csv", {
                    "filename": "results",
                    "headers": ["intent", "configuration", "time"]
                })
            except Exception as e:
                print(f"Warning: Failed to initialize CSV logging: {e}")

            flm_server = self.mcp_client.server("flm")

            # -----------------------------------
            # 1. RAG Retrieval
            # -----------------------------------
            arch_base = self._build_arch_base(requirement.get("rag_filters", []))
            context.rag_arch_base = arch_base

            # Intent normalization (v17 behavior): enrich the requirement into a
            # retrieval query, then prepend it to the RAW requirement. Keeping the
            # original text preserves the device names (R1, SW1, ...) the Colab
            # retriever keys on for router/switch scope inference.
            normalized_intent = self._normalize_intent(context, flm_server)
            if normalized_intent:
                semantic_query = (normalized_intent + "\n" + context.intent).strip()
            else:
                semantic_query = context.intent
            context.normalized_intent = normalized_intent or context.intent
            context.rag_query = semantic_query

            retrieval_response = flm_server.call_tool(
                "retrieve_chunks",
                {
                    "semantic_query": semantic_query,
                    # Raw requirement so the retriever infers the router/switch
                    # scope from it (v17), not from the enriched semantic_query.
                    "requirement": context.intent,
                    "os": arch_base["os"],
                    "version": arch_base["version"],
                    "device_type": arch_base["device_type"],
                    "product": arch_base["product"],
                    # Quota per detected device type, not a global cap: a requirement
                    # touching a router and a switch retrieves 3 chunks for each.
                    "k": 3
                }
            )

            if retrieval_response.get("isError", True):
                context.update("RAG_RETRIEVAL", {"status": "failed", "data": retrieval_response})
                context.mark_failed("RAG retrieval failed.")
                return context

            retrieval_data = self._extract_retrieval_data(retrieval_response)
            retrieved_context = self._extract_retrieved_context(retrieval_data)
            context.retrieved_chunks = retrieval_data.get("chunks", [])
            context.retrieved_context = retrieved_context

            context.update("RAG_RETRIEVAL", {
                "status": "success",
                "data": retrieval_data
            })

            # -----------------------------------
            # 2. Configuration Generation
            # -----------------------------------
            # v17: use the RAG prompt only when chunks were retrieved; otherwise
            # fall back to the no-RAG prompt instead of injecting a
            # "No relevant documentation" block.
            if context.retrieved_chunks:
                config_prompt = self.prompt_manager.build_config_prompt_rag(
                    context,
                    retrieved_context
                )
            else:
                config_prompt = self.prompt_manager.build_config_prompt_no_rag(
                    context
                )

            config_response = flm_server.call_tool(
                "send_prompt",
                {"prompt": config_prompt}
            )

            context.update(config_prompt, config_response)

            if config_response.get("isError", True):
                context.mark_failed("Configuration generation failed.")
                return context

            # -----------------------------------
            # 3. Verification
            # -----------------------------------
            verification_success = self._verify_configuration(context)

            # -----------------------------------
            # 4. Refinement Loop (if needed)
            # -----------------------------------
            if not verification_success:
                self._refinement_loop(context)

            if context.state != ExecutionState.SUCCESS:
                if context.state == ExecutionState.RUNNING:
                    context.mark_failed("Execution finished without a successful verification.")

            return context

            '''
            # -----------------------------------
            # 1a. Classification
            # -----------------------------------
            classifier_prompt = self.prompt_manager.build_classifier_prompt(context)

            classifier_response = flm_server.call_tool(
                "send_prompt",
                {"prompt": classifier_prompt}
            )

            # Record this step in the execution history.
            context.update(classifier_prompt, classifier_response)

            if classifier_response.get("isError", True):
                context.mark_failed("Classification failed.")
                return context  # Exit early if classification fails.

            # -----------------------------------
            # 1b. Step Generation
            # -----------------------------------
            tasks_prompt = self.prompt_manager.build_tasks_prompt(context)

            tasks_response = flm_server.call_tool(
                "send_prompt",
                {"prompt": tasks_prompt}
            )

            # Record this step in the execution history.
            context.update(tasks_prompt, tasks_response)

            if tasks_response.get("isError", True):
                context.mark_failed("Step generation failed.")
                return context  # Exit early if step generation fails. 
            '''

            # -----------------------------------
            # 2. Configuration Generation
            # -----------------------------------
            config_prompt = self.prompt_manager.build_config_prompt_v2(context)

            config_response = flm_server.call_tool(
                "send_prompt",
                {"prompt": config_prompt}
            )

            # Record the configuration generation step.
            context.update(config_prompt, config_response)

            if config_response.get("isError", True):
                context.mark_failed("Configuration generation failed.")
                return context  # Exit early if generation fails.

            # -----------------------------------
            # 3. Verification
            # -----------------------------------
            verification_success = self._verify_configuration(context)

            # -----------------------------------
            # 4️. Refinement Loop (if needed)
            # -----------------------------------
            if not verification_success:
                self._refinement_loop(context)

            # Final evaluation: if after all steps (including refinement) the state is not SUCCESS,
            # mark it as failed. This handles cases where the refinement loop exits without success.
            if context.state != ExecutionState.SUCCESS:
                # Avoid overwriting a more specific state like MAX_ITERATIONS_REACHED.
                if context.state == ExecutionState.RUNNING:
                    context.mark_failed("Execution finished without a successful verification.")

        except Exception as e:
            context.mark_failed(str(e))

        finally:
            end_time = time.time()
            elapsed_time = end_time - start_time
            
            # Extraer intent y configuración final
            intent_str = str(requirement.get("intent", requirement.get("text", requirement))) if isinstance(requirement, dict) else str(requirement)
            final_config = getattr(context, "generated_config", "")
            
            # Registrar resultados en el CSV
            try:
                csv_server = self.mcp_client.server("csv")
                if not csv_server._initialized:
                    csv_server.start()
                    csv_server.initialize()
                csv_server.call_tool("append_row", {
                    "filename": "results.csv",
                    "row": {
                        "intent": intent_str,
                        "configuration": final_config,
                        "time": str(round(elapsed_time, 2))
                    }
                })
            except Exception as e:
                print(f"Warning: Failed to log results to CSV: {e}")

            # Always save the execution context, regardless of success or failure.
            self.result_logger.save(context)

        return context

    # ==========================================================
    # PRIVATE METHODS
    # ==========================================================

    def _build_arch_base(self, rag_filters: list[dict]) -> dict:
        """
        Builds the ARCH_BASE payload sent to the Colab retrieval endpoint.
        Multiple CLI selections are merged into one architecture scope.
        """
        if not rag_filters:
            raise ValueError("At least one RAG filter selection is required.")

        arch_base = {
            "os": [],
            "version": [],
            "device_type": [],
            "product": [],
        }

        for item in rag_filters:
            self._append_unique(arch_base["device_type"], item.get("device_type"))
            self._append_unique(arch_base["product"], item.get("product"))
            self._append_unique(arch_base["os"], item.get("operating_system") or item.get("os"))

            for version in self._expand_version_family(item.get("version")):
                self._append_unique(arch_base["version"], version)

        missing = [key for key, value in arch_base.items() if not value]
        if missing:
            raise ValueError(f"Incomplete RAG filter selection. Missing: {', '.join(missing)}")

        return arch_base

    def _append_unique(self, values: list, value):
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                self._append_unique(values, item)
            return
        text = str(value).strip()
        if text and text.lower() != "not defined" and text not in values:
            values.append(text)

    def _expand_version_family(self, version) -> list[str]:
        """
        Expands a selected version into the version family variants used by
        the RAG metadata, e.g. 17.12.1 -> 17.12.1, 17.12.x, 17.x.
        """
        if not version:
            return []

        text = str(version).strip()
        if not text or text.lower() == "not defined":
            return []

        parts = text.split(".")
        versions = [text]

        if len(parts) >= 2:
            family = f"{parts[0]}.{parts[1]}.x"
            if family not in versions:
                versions.append(family)

        major = f"{parts[0]}.x"
        if major not in versions:
            versions.append(major)

        return versions

    def _extract_response_text(self, response: dict) -> str:
        content = response.get("content", [])
        if content and isinstance(content, list):
            text = content[0].get("text", "")
            if isinstance(text, str):
                return text

        data = response.get("data")
        if isinstance(data, str):
            return data

        return ""

    def _normalize_intent(self, context, flm_server) -> str:
        """
        Asks the FLM to enrich the requirement into a retrieval query
        (v17 normalize_intent). Returns the cleaned text, or "" on any
        failure so the caller falls back to the raw requirement.
        """
        try:
            prompt = self.prompt_manager.build_intent_normalization_prompt(context)
            response = flm_server.call_tool(
                "send_prompt",
                {"prompt": prompt, "max_new_tokens": MAX_NEW_TOKENS_INTENT},
            )
            if response.get("isError", True):
                return ""
            text = self._clean_normalized_intent(self._extract_response_text(response))
            return text
        except Exception as exc:
            print(f"Warning: intent normalization failed: {exc}")
            return ""

    def _clean_normalized_intent(self, text: str) -> str:
        """
        Cleans the FLM's normalized-intent output, identical to the v17
        clean_normalized_intent: strips code fences and any leading meta
        prefix/phrase the model may prepend despite the prompt forbidding it.
        """
        text = str(text).strip()
        text = text.replace("```json", "").replace("```", "").strip()
        for prefix in [
            "Normalized intent:", "Intent:", "Output:", "Result:",
            "Query:", "Normalize:", "Find documentation:",
        ]:
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip()
        if text.lower().startswith("normalize "):
            text = text[len("normalize "):].strip()
        if text.lower().startswith("find documentation for "):
            text = text[len("find documentation for "):].strip()
        return text

    def _clean_model_text(self, text: str) -> str:
        text = str(text or "").strip()
        text = text.replace("```json", "").replace("```", "").strip()
        for prefix in ["Normalized intent:", "Intent:", "Output:", "Result:", "Query:"]:
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip()
        return text

    def _extract_retrieval_data(self, response: dict) -> dict:
        struct = response.get("structuredContent")
        if isinstance(struct, dict) and struct:
            return struct

        data = response.get("data")
        if isinstance(data, dict):
            return data

        text = self._extract_response_text(response)
        if not text:
            return {"retrieved_context": "No relevant Cisco documentation was retrieved."}

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        return {"retrieved_context": text}

    def _extract_retrieved_context(self, retrieval_data: dict) -> str:
        retrieved_context = retrieval_data.get("retrieved_context")
        if isinstance(retrieved_context, str) and retrieved_context.strip():
            return retrieved_context

        chunks = retrieval_data.get("chunks", [])
        if chunks:
            return self._format_retrieved_chunks(chunks)

        return "No relevant Cisco documentation was retrieved."

    def _format_retrieved_chunks(self, chunks: list[dict]) -> str:
        blocks = []
        for index, chunk in enumerate(chunks, start=1):
            parts = [
                f"[DOCUMENTATION CHUNK {index}]",
                f"Score: {float(chunk.get('score', 0.0)):.4f}",
                f"Retrieval scope: {chunk.get('retrieval_scope', '')}",
                f"Device type: {chunk.get('device_type', '')}",
                f"Product: {chunk.get('product', '')}",
                f"OS: {chunk.get('os', '')}",
                f"Version: {chunk.get('version', '')}",
                f"Guide: {chunk.get('configuration_guide', '')}",
                f"Chapter: {chunk.get('chapter', '')}",
                f"Section: {chunk.get('section', '')}",
                "",
                "Commands:",
                str(chunk.get("commands", "")),
                "",
                "Examples:",
                str(chunk.get("examples", "")),
            ]
            blocks.append("\n".join(parts).strip())

        return "\n\n".join(blocks)

    def _check_parse_status(self, response: dict) -> bool:
        """
        Detecta si existe al menos un archivo/dispositivo con status distinto de PASSED
        en la respuesta de fileParseStatus explorando el valor estructurado o crudo.
        """
        # 1. Búsqueda limpia en el árbol de resultados devuelto por Batfish MCP
        struct = response.get("structuredContent", {})
        if "results" in struct:
            for r in struct.get("results", []):
                st = str(r.get("status", "")).upper()
                if st in ["FAILED", "PARTIALLY_UNRECOGNIZED", "UNKNOWN"]:
                    return False
        
        # 2. Búsqueda transversal por si el schema viene distinto (anidado)
        data_str = json.dumps(response).upper()
        if '"STATUS": "FAILED"' in data_str or '"STATUS": "PARTIALLY_UNRECOGNIZED"' in data_str or '"STATUS": "UNKNOWN"' in data_str:
            return False
            
        return True

    def _has_records(self, response: dict) -> bool:
        """
        Detecta si existen registros en parseWarning o initIssues explorando
        su atributo estructurado para saber si hay data a corregir.
        """
        # Chequeo estructural sobre MCP
        struct = response.get("structuredContent", {})
        if "results" in struct:
            return len(struct.get("results", [])) > 0
            
        # Fallback de texto
        content = response.get("content", [])
        if content and len(content) > 0:
            text = content[0].get("text", "").strip()
            # Si es un array/diccionario vacío o string vacío, no hay registros
            if text in ["[]", "{}", ""] or not text:
                return False
            return True
        return False

    def _extract_failed_files(self, response: dict):
        """
        Extrae y devuelve únicamente los registros de dispositivos/archivos que 
        no tengan status PASSED.
        """
        failed_items = []
        struct = response.get("structuredContent", {})
        if "results" in struct:
            for r in struct.get("results", []):
                st = str(r.get("status", "")).upper()
                if st in ["FAILED", "PARTIALLY_UNRECOGNIZED", "UNKNOWN"]:
                    failed_items.append(r)
            if failed_items:
                return failed_items
                
        # Fallback: si no extrajo nada pero hay un fallo general
        return response

    def _build_feedback(self, status_resp, warnings_resp, issues_resp) -> dict:
        """
        Construye la retroalimentación en un formato más puntual y directo
        para disminuir la longitud del JSON que recibe el modelo.
        """
        failed_status = self._extract_failed_files(status_resp)
        if isinstance(failed_status, dict):
            failed_status = failed_status.get("structuredContent", {}).get("results", [])
            
        failed_files = {r.get("file_name", ""): r.get("status", "UNKNOWN") for r in failed_status if isinstance(r, dict)}
        
        compact_errors = []
        
        # Extraer warnings
        warnings_struct = warnings_resp.get("structuredContent", {}) if isinstance(warnings_resp, dict) else {}
        warnings_results = warnings_struct.get("results", [])
        
        if warnings_results:
            for w in warnings_results:
                filename = w.get("filename", "")
                if not filename and failed_files:
                     filename = list(failed_files.keys())[0]
                     
                compact_errors.append({
                    "File": filename,
                    "Status": failed_files.get(filename, "UNKNOWN"),
                    "Line": w.get("line", "Unknown"),
                    "Invalid line": w.get("text", "").strip() if w.get("text") else f"Line {w.get('line', 'Unknown')}",
                    "Parser context": w.get("parser_context", ""),
                    "Reason": w.get("comment", "Syntax is unrecognized")
                })
        else:
            # Extraer issues si no hubo warnings
            issues_struct = issues_resp.get("structuredContent", {}) if isinstance(issues_resp, dict) else {}
            issues_results = issues_struct.get("results", [])
            
            if issues_results:
                for issue in issues_results:
                    source_lines = issue.get("source_lines", [])
                    filename = source_lines[0].split(":")[0] if source_lines else (list(failed_files.keys())[0] if failed_files else "Unknown")

                    compact_errors.append({
                        "File": filename,
                        "Status": failed_files.get(filename, "UNKNOWN"),
                        "Line": source_lines[0] if source_lines else "Unknown",
                        "Invalid line": issue.get("line_text", "").strip(),
                        "Parser context": issue.get("parser_context", ""),
                        "Reason": issue.get("details", "")
                    })
            else:
                # Fallback si no hay warnings ni issues
                for fname, status in failed_files.items():
                    compact_errors.append({
                        "File": fname,
                        "Status": status,
                        "Invalid line": "N/A",
                        "Reason": "Parsing failed or partially unrecognized."
                    })

        return {
            "errors": compact_errors,
            "instruction": "Review the provided errors and provide specific correction instructions for the failed files."
        }

    def should_use_init_issues(self, parse_status_results, parse_warning_results):
        failed_files = [
            r for r in parse_status_results
            if r["status"] in {"PARTIALLY_UNRECOGNIZED", "FAILED", "UNKNOWN"}
        ]

        if not failed_files:
            return False

        warnings = parse_warning_results.get("results", [])

        if not warnings:
            return True 

        for w in warnings:
            text = (w.get("text") or "").strip()
            comment = (w.get("comment") or "").strip()
            line = w.get("line")

            if not text and line is None:
                return True

            if not comment:
                return True

        return False

    def _verify_configuration(self, context: ExecutionContext) -> bool:
        """
        Sends generated configuration to verifier tool via MCP.
        Executes fileParseStatus and conditionally parseWarning and initIssues.
        Builds feedback and updates context.
        """
        from utils.create_snapshot import create_snapshot_from_string
        from utils.create_snapshot import merge_snapshots_overlay
        import os

        # 1. Obtener la configuración parcial generada por el LLM
        llm_config_output = context.final_result
        if isinstance(llm_config_output, str):
            # Limpieza de la salida del LLM, que a veces viene como un string JSON-escaped
            if llm_config_output.startswith('"') and llm_config_output.endswith('"'):
                try:
                    llm_config_output = json.loads(llm_config_output)
                except Exception:
                    pass
            llm_config_output = llm_config_output.replace("\\n", "\n").replace('\\"', '"').replace("\\t", "\t")

        # Guardamos la configuración limpia para registrarla luego en el CSV
        context.generated_config = str(llm_config_output) if llm_config_output else ""

        # Pre-check de placeholders ANTES de Batfish: el parser descartaría en
        # silencio cualquier línea con <...>, produciendo un falso PASSED con la
        # línea evaporada. La detectamos aquí y la reportamos como fallo explícito
        # y accionable, sin llegar a parsear ni a Batfish.
        placeholder_errors = self._detect_placeholders(llm_config_output)
        if placeholder_errors:
            feedback_data = {
                "errors": placeholder_errors,
                "instruction": "Replace each placeholder (text in angle brackets) with a concrete, valid value. Change only the flagged line(s).",
            }
            context.update("VERIFICATION_CALL", {"status": "failed", "data": feedback_data})
            return False

        # Parsear los comandos CLI devueltos por el LLM a formato Batfish
        from utils.batfish_parser import BatfishPredictionParser
        parser = BatfishPredictionParser(none_value="")
        parsed_llm_config = parser.parse_prediction(llm_config_output)
        if not str(parsed_llm_config or "").strip():
            # Feedback accionable: intenta diagnosticar POR QUE quedo UNPARSEABLE
            # (causa dominante: comandos de sub-modo sin su linea 'interface'/'router').
            feedback_data = self._diagnose_unparseable(llm_config_output) or {
                "errors": [{
                    "File": "generated configuration",
                    "Status": "UNPARSEABLE",
                    "Invalid line": "N/A",
                    "Reason": "The generated output could not be converted into a Batfish verification overlay."
                }],
                "instruction": "Repair the configuration while preserving the original requirement and valid commands."
            }
            context.update("VERIFICATION_CALL", {"status": "failed", "data": feedback_data})
            return False

        # 2. Obtener la configuración base completa desde el PromptManager
        base_config_text = self.prompt_manager.get_base_config_text()

        # 3. Fusionar la base con los cambios parseados del LLM para obtener el snapshot final
        final_config_data = merge_snapshots_overlay(base_config_text, parsed_llm_config)

        # 4. Crear el snapshot para Batfish a partir de la configuración fusionada
        batfish_server = self.mcp_client.server("batfish")
        
        snapshot_dir = os.path.join(os.path.expanduser("~"), "Documents", "snapshot_verify")
        zip_path = create_snapshot_from_string(final_config_data, base_dir=snapshot_dir)
        
        if not zip_path:
            context.update("VERIFICATION_CALL", {"status": "failed", "data": "Error creating snapshot"})
            return False

        load_response = batfish_server.call_tool("load_snapshot", {"zip_path": zip_path})
        
        # 1. fileParseStatus siempre debe ejecutarse
        status_response = batfish_server.call_tool("file_parse_status", {})
        
        # 2. Detectar si todos son PASSED
        all_passed = self._check_parse_status(status_response)

        # 3. Si todos son PASSED: finalizar loop como exitoso
        if all_passed:
            verification_data = {
                "status": status_response,
                "message": "Validacion estructural exitosa: Batfish pudo parsear el snapshot con el overlay generado."
            }
            context.update("VERIFICATION_CALL", {"status": "success", "data": verification_data})
            context.mark_success()
            return True

        # 4. Si hay algun no-PASSED: ejecutar parseWarning
        warnings_response = batfish_server.call_tool("parse_warning", {"aggregate_duplicates": True})
        
        # 5. Condicionalmente ejecutar initIssues basado en should_use_init_issues
        parse_status_results = status_response.get("structuredContent", {}).get("results", [])
        parse_warning_results = warnings_response.get("structuredContent", {})
        
        issues_response = {}
        if self.should_use_init_issues(parse_status_results, parse_warning_results):
            issues_response = batfish_server.call_tool("init_issues", {})

        # 6. Construir la retroalimentación en formato compacto
        feedback_data = self._build_feedback(status_response, warnings_response, issues_response)
        
        # Falló la verificacion, proveemos el feedback detallado
        context.update("VERIFICATION_CALL", {"status": "failed", "data": feedback_data})
        return False

    def _refinement_loop(self, context: ExecutionContext):
        """
        Iteratively refines configuration if verification fails.

        Surgical strategy: when Batfish flags concrete invalid lines, the model
        is asked ONLY for the corrected replacement of those lines, and the code
        rebuilds the config replacing just those lines (every other line stays
        byte-for-byte identical). This guarantees the model can only change the
        line(s) it was told to change. If the failure has no concrete line to
        target (e.g. the whole output was UNPARSEABLE), it falls back to a full
        regeneration.
        """
        flm_server = self.mcp_client.server("flm")

        for _ in range(self.max_refinement_iterations):
            previous_config = context.final_result or getattr(context, "generated_config", "") or ""
            feedback = self._extract_latest_verification_report(context)
            invalid_items = self._resolve_invalid_lines(previous_config, feedback)

            if invalid_items:
                # --- Modo quirúrgico: corregir solo las líneas señaladas ---
                fix_prompt = self.prompt_manager.build_line_fix_prompt(context, invalid_items)
                fix_response = flm_server.call_tool("send_prompt", {"prompt": fix_prompt})
                context.update(fix_prompt, fix_response)

                if fix_response.get("isError", True):
                    continue

                corrected_output = self._extract_response_text(fix_response)
                patched_config = self._apply_line_fixes(previous_config, invalid_items, corrected_output)

                # La config verificada es la anterior con SOLO las líneas malas reemplazadas.
                context.final_result = patched_config
                context.generated_config = patched_config

                # Guardar la config reconstruida en la iteración (para inspección/display):
                # el modelo solo devuelve la línea, pero esto es lo que realmente se verifica.
                if context.iterations:
                    context.iterations[-1]["patched_config"] = patched_config
            else:
                # --- Fallback: regeneración completa (p.ej. salida UNPARSEABLE) ---
                refinement_prompt = self.prompt_manager.build_refinement_prompt(context)
                refinement_response = flm_server.call_tool("send_prompt", {"prompt": refinement_prompt})
                context.update(refinement_prompt, refinement_response)

                if refinement_response.get("isError", True):
                    continue

            verification_success = self._verify_configuration(context)
            if verification_success:
                return

        # If the loop finishes without a successful verification, update the state.
        if not context.is_success():
            context.state = ExecutionState.MAX_ITERATIONS_REACHED

    def _detect_placeholders(self, config_text: str) -> list[dict]:
        """
        Scans the raw model output for placeholder tokens (text in angle
        brackets, e.g. <password>). These would be silently dropped by the
        parser, so we flag them BEFORE parsing/Batfish as an explicit, actionable
        failure. Returns a list of error dicts in the same shape as the Batfish
        feedback, so the surgical refinement loop can target them line by line.
        """
        import re

        placeholder_re = re.compile(r"<[^>]+>")
        errors = []
        for raw_line in str(config_text or "").splitlines():
            line = raw_line.strip()
            if not line or not placeholder_re.search(line):
                continue

            command = line.split("#", 1)[1].strip() if "#" in line else line
            errors.append({
                "File": "generated configuration",
                "Status": "PLACEHOLDER",
                "Invalid line": command,
                "Parser context": "",
                "Reason": "Contains a placeholder in angle brackets (e.g. <password>). Use a concrete, valid value instead.",
            })
        return errors

    def _diagnose_unparseable(self, config_text: str):
        """
        Diagnoses the most common UNPARSEABLE cause: sub-mode commands
        (config-if, config-router, ...) emitted WITHOUT their mode-entering
        (block-opener) command, so the parser cannot know their context (e.g.
        which interface) and drops everything. Returns an actionable feedback
        dict, or None if the cause can't be pinpointed (caller uses a generic msg).
        """
        from utils.batfish_parser import PROMPT_RE, BatfishPredictionParser

        orphaned = []
        has_opener = False
        for raw_line in str(config_text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = PROMPT_RE.match(line)
            if not match:
                continue
            mode = (match.group(2) or "").strip().lower()
            command = match.group(3).strip()
            if not command:
                continue
            if BatfishPredictionParser._is_block_opener(command):
                has_opener = True
            elif mode not in ("", "config"):
                orphaned.append(command)

        if orphaned and not has_opener:
            return {
                "errors": [{
                    "File": "generated configuration",
                    "Status": "UNPARSEABLE",
                    "Invalid line": "N/A",
                    "Missing": "mode-entering command (e.g. 'interface <name>' or 'router <proto> <id>')",
                    "Orphaned sub-mode commands": orphaned[:6],
                    "Reason": "Sub-mode commands were emitted without their parent mode-entering command, "
                              "so the parser could not determine their context (e.g. which interface).",
                }],
                "instruction": "Regenerate the FULL configuration. Before each sub-mode command include its "
                               "mode-entering command (e.g. put 'interface FastEthernet0/1' before its (config-if)# "
                               "commands, or 'router ospf 1' before its (config-router)# commands). Keep every valid command.",
            }
        return None

    def _extract_latest_verification_report(self, context: ExecutionContext) -> dict:
        """Returns the feedback 'data' from the most recent verification call."""
        for iteration in reversed(context.iterations):
            if iteration.get("prompt") == "VERIFICATION_CALL":
                return iteration.get("response", {}).get("data", {})
        return {}

    def _resolve_invalid_lines(self, previous_config: str, feedback) -> list[dict]:
        """
        Maps each Batfish error to its full CLI line (with prompt prefix) inside
        the previous config transcript, so the model can be asked to fix exactly
        that line. Errors without a concrete line (Invalid line == 'N/A') are
        skipped and handled by the full-regeneration fallback.
        """
        errors = feedback.get("errors", []) if isinstance(feedback, dict) else []
        prev_lines = str(previous_config or "").splitlines()

        def norm(text: str) -> str:
            return " ".join(str(text or "").split()).lower()

        items = []
        for error in errors:
            invalid = str(error.get("Invalid line", "")).strip()
            if not invalid or invalid.upper() == "N/A":
                continue

            target = norm(invalid)
            full_line = None
            for line in prev_lines:
                command = line.split("#", 1)[1] if "#" in line else line
                if norm(command) == target:
                    full_line = line.strip()
                    break

            items.append({
                "full_line": full_line if full_line is not None else invalid,
                "parser_context": error.get("Parser context", ""),
                "reason": error.get("Reason", ""),
            })
        return items

    def _apply_line_fixes(self, previous_config: str, invalid_items: list[dict], corrected_output: str) -> str:
        """
        Rebuilds the config by replacing ONLY the flagged lines with the model's
        corrected lines (positional, in order). Every other line is preserved
        exactly. If a corrected line lacks a CLI prompt prefix, the original
        line's prefix is reused.
        """
        prev_lines = str(previous_config or "").splitlines()
        corrected = [ln.strip() for ln in str(corrected_output or "").splitlines() if ln.strip()]

        def norm(text: str) -> str:
            return " ".join(str(text or "").split()).lower()

        pending = [norm(item["full_line"]) for item in invalid_items]

        result = []
        used = 0
        for line in prev_lines:
            if norm(line) in pending and used < len(corrected):
                fix = corrected[used]
                used += 1
                pending.remove(norm(line))
                if "#" not in fix and "#" in line:
                    fix = line.split("#", 1)[0] + "#" + fix
                result.append(fix)
            else:
                result.append(line)

        return "\n".join(result)
