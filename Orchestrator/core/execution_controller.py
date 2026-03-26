# core/execution_controller.py

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

    def _verify_configuration(self, context: ExecutionContext) -> bool:
        """
        Sends generated configuration to verifier tool via MCP.
        Updates the context with the verification result.
        Sets the final context state to SUCCESS if verification passes.
        """
        from utils.create_snapshot import create_snapshot_from_string
        import os
        import json

        # text response produced by FLM in Config Gen step
        config_data = context.final_result
        
        # Corrección: Extraer los saltos de línea y formateo doble explícitamente
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
        status_response = batfish_server.call_tool("file_parse_status", {})
        # warnings_response = batfish_server.call_tool("parse_warning", {"aggregate_duplicates": True})
        issues_response = batfish_server.call_tool("init_issues", {})
        
        verification_response = {
            "load": load_response,
            "status": status_response,
            # "warnings": warnings_response,
            "issues": issues_response
        }

        # Determinamos si fue exitoso asumiendo que el flag de isError indica errores criticos
        has_errors = issues_response.get("isError", False)
        
        if not has_errors:
            record_response = {"status": "success", "data": verification_response}
        else:
            record_response = {"status": "failed", "data": verification_response}

        context.update("VERIFICATION_CALL", record_response)

        if not has_errors:
            context.mark_success()
            return True

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
            if refinement_response.get("status") != "success":
                continue

            # Re-verify the newly generated configuration.
            verification_success = self._verify_configuration(context)

            # If verification passes, the job is done. Exit the loop.
            if verification_success:
                return

        # If the loop finishes without a successful verification, update the state.
        if not context.is_success():
            context.state = ExecutionState.MAX_ITERATIONS_REACHED
