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
        1. Classification
        2. Step Generation
        3. Configuration Generation
        4. Verification
        5. Refinement loop (if needed)
        """
        # Create a unique context to track this specific execution's state and history.
        context = ExecutionContext(requirement)

        try:
            flm_server = self.mcp_client.server("flm")

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

            # -----------------------------------
            # 2. Configuration Generation
            # -----------------------------------
            config_prompt = self.prompt_manager.build_config_prompt(context)

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
            # Always save the execution context, regardless of success or failure.
            self.result_logger.save(context)

        return context

    # ==========================================================
    # PRIVATE METHODS
    # ==========================================================

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
        Construye la retroalimentación estructurada para el modelo, priorizando
        parseWarning y luego initIssues, ordenando corregir solo archivos fallidos.
        """
        failed_status = self._extract_failed_files(status_resp)
        
        feedback = {
            "failed_files_parse_status": failed_status,
            "instruction": "" # review the failed files and provide specific correction instructions, prioritizing parseWarning issues over initIssues, and only for the files that failed in fileParseStatus.
        }
        
        if self._has_records(warnings_resp):
            feedback["primary_correction_source_parseWarning"] = warnings_resp
            
        if self._has_records(issues_resp):
            feedback["complementary_context_initIssues"] = issues_resp
            
        return feedback

    def _verify_configuration(self, context: ExecutionContext) -> bool:
        """
        Sends generated configuration to verifier tool via MCP.
        Executes fileParseStatus and conditionally parseWarning and initIssues.
        Builds feedback and updates context.
        """
        from utils.create_snapshot import create_snapshot_from_string
        import os

        config_data = context.final_result
        if isinstance(config_data, str):
            if config_data.startswith('"') and config_data.endswith('"'):
                try:
                    config_data = json.loads(config_data)
                except Exception:
                    pass
            config_data = config_data.replace("\\n", "\n").replace('\\"', '"').replace("\\t", "\t")

        batfish_server = self.mcp_client.server("batfish")
        
        snapshot_dir = os.path.join(os.path.expanduser("~"), "Documents", "snapshot_verify")
        zip_path = create_snapshot_from_string(config_data, base_dir=snapshot_dir)

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
                "message": "Validacion exitosa: todos los estados son PASSED."
            }
            context.update("VERIFICATION_CALL", {"status": "success", "data": verification_data})
            context.mark_success()
            return True

        # 4. Si hay algun no-PASSED: ejecutar parseWarning e initIssues
        warnings_response = batfish_server.call_tool("parse_warning", {"aggregate_duplicates": True})
        issues_response = batfish_server.call_tool("init_issues", {})

        # 5 y 6. Construir la retroalimentación priorizada
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
