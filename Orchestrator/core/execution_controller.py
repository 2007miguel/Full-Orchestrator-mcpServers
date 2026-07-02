# core/execution_controller.py

import json
from models.execution_context import ExecutionContext
from models.state import ExecutionState


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
            # 1. Intent Normalization
            # -----------------------------------
            arch_base = self._build_arch_base(requirement.get("rag_filters", []))
            context.rag_arch_base = arch_base

            normalization_prompt = self.prompt_manager.build_intent_normalization_prompt(
                context,
                arch_base
            )
            normalization_response = flm_server.call_tool(
                "send_prompt",
                {"prompt": normalization_prompt}
            )

            context.update(normalization_prompt, normalization_response)

            if normalization_response.get("isError", True):
                context.mark_failed("Intent normalization failed.")
                return context

            normalized_intent = self._clean_model_text(
                self._extract_response_text(normalization_response) or context.intent
            )
            context.normalized_intent = normalized_intent

            # -----------------------------------
            # 2. RAG Retrieval
            # -----------------------------------
            semantic_query = normalized_intent
            context.rag_query = semantic_query

            retrieval_response = flm_server.call_tool(
                "retrieve_chunks",
                {
                    "semantic_query": semantic_query,
                    "os": arch_base["os"],
                    "version": arch_base["version"],
                    "device_type": arch_base["device_type"],
                    "product": arch_base["product"],
                    "k": 4
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
            # 3. Configuration Generation
            # -----------------------------------
            config_prompt = self.prompt_manager.build_config_prompt_rag(
                context,
                retrieved_context
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
            # 4. Verification
            # -----------------------------------
            verification_success = self._verify_configuration(context)

            # -----------------------------------
            # 5. Refinement Loop (if needed)
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

    def _clean_model_text(self, text: str) -> str:
        text = str(text or "").strip()
        text = text.replace("```json", "").replace("```", "").strip()
        for prefix in ["Normalized intent:", "Intent:", "Output:", "Result:", "Query:"]:
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip()
        return text

    def _build_rag_query(self, requirement: str, normalized_intent: str) -> str:
        return (
            "Normalized intent:\n"
            f"{normalized_intent}\n\n"
            "Original requirement:\n"
            f"{requirement}"
        ).strip()

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
                    "Invalid line": w.get("text", "").strip() if w.get("text") else f"Line {w.get('line', 'Unknown')}",
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
                        "Invalid line": issue.get("line_text", "").strip(),
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

        # Parsear los comandos CLI devueltos por el LLM a formato Batfish
        from utils.batfish_parser import BatfishPredictionParser
        parser = BatfishPredictionParser(none_value="")
        parsed_llm_config = parser.parse_prediction(llm_config_output)
        if not str(parsed_llm_config or "").strip():
            feedback_data = {
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
        """
        flm_server = self.mcp_client.server("flm")

        for _ in range(self.max_refinement_iterations):

            refinement_prompt = self.prompt_manager.build_refinement_prompt(context)

            refinement_response = flm_server.call_tool(
                "send_prompt",
                {"prompt": refinement_prompt}
            )

            # Record the refinement attempt.
            context.update(refinement_prompt, refinement_response)

            # If the model failed to generate a corrected configuration, try again.
            if refinement_response.get("isError", True):
                continue

            # Re-verify the newly generated configuration.
            verification_success = self._verify_configuration(context)

            # If verification passes, the job is done. Exit the loop.
            if verification_success:
                return

        # If the loop finishes without a successful verification, update the state.
        if not context.is_success():
            context.state = ExecutionState.MAX_ITERATIONS_REACHED
