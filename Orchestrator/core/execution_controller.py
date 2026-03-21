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
        1. Combined intent processing (classification + steps)
        2. Configuration generation
        3. Verification
        4. Refinement loop (if needed)
        """
        # Create a unique context to track this specific execution's state and history.
        context = ExecutionContext(requirement)

        try:
            # -----------------------------------
            # 1️. Classification + Step Generation (Combined Intent Processing)
            # -----------------------------------
            combined_prompt = self.prompt_manager.build_combined_prompt(context)

            combined_response = self.mcp_client.call_tool(
                tool_name="flm",
                payload={"prompt": combined_prompt}
            )

            # Record this step in the execution history.
            context.update(combined_prompt, combined_response)

            if combined_response.get("status") != "success":
                context.mark_failed("Combined intent processing failed.")
                return context  # Exit early if the first step fails.

            # -----------------------------------
            # 2️. Configuration Generation
            # -----------------------------------
            config_prompt = self.prompt_manager.build_config_prompt(context)

            config_response = self.mcp_client.call_tool(
                tool_name="flm",
                payload={"prompt": config_prompt}
            )

            # Record the configuration generation step.
            context.update(config_prompt, config_response)

            if config_response.get("status") != "success":
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
        # The configuration to verify is the last successful data stored in the context.
        config_data = context.final_result

        verification_response = self.mcp_client.call_tool(
            tool_name="verifier",
            payload={"configuration": config_data}
        )

        # Record the verification attempt in the context.
        context.update("VERIFICATION_CALL", verification_response)

        if verification_response.get("status") == "success":
            # Explicitly mark the entire execution as successful.
            context.mark_success()
            return True

        return False

    def _refinement_loop(self, context: ExecutionContext):
        """
        Iteratively refines configuration if verification fails.
        """

        for _ in range(self.max_refinement_iterations):

            refinement_prompt = self.prompt_manager.build_refinement_prompt(context)

            refinement_response = self.mcp_client.call_tool(
                tool_name="flm",
                payload={"prompt": refinement_prompt}
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
