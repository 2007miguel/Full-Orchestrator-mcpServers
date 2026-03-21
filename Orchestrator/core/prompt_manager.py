import json
from pathlib import Path


class PromptManager:
    """
    Acts as a factory for creating specific prompts for each step of the orchestration pipeline.

    It isolates the logic of prompt construction from the main execution flow,
    making the system more modular. It loads templates and static data at initialization
    to be efficient.
    """

    def __init__(self, templates_path: str):
        """
        Initializes the PromptManager by loading all necessary templates and static data.
        """
        self.templates_path = Path(templates_path)

        # Pre-load all templates from disk to avoid repeated I/O on each call.
        self.classifier_template = self._load_text("classifier_and_tasks_prompt.txt")
        self.config_template = self._load_text("config_prompt.txt")
        self.repair_template = self._load_text("post_verification_prompt.txt")

        # Load static topology once
        self.topology = self._load_json("network_topology.json")

    # ==========================================================
    # PUBLIC METHODS (Used by ExecutionController)
    # ==========================================================

    def build_combined_prompt(self, context):
        """
        Builds prompt for classification + high-level task decomposition.
        This is the first step in the pipeline.
        """
        # Extract the user's natural language requirement.
        user_text = context.intent
        topology_json = json.dumps(self.topology, indent=2)

        # Inject the dynamic data into the classifier template.
        prompt = self.classifier_template.format(
            user_text=user_text,
            topology_json=topology_json
        )

        return prompt

    def build_config_prompt(self, context):
        """
        Builds prompt for configuration generation.
        Requires previous combined output stored in context.
        """
        # Find the successful output from the previous (classification) step.
        plan_ir = self._extract_latest_success_data(context)

        # Inject the classification output (context plan) into the config template.
        prompt = self.config_template.format(
            context_plan_json=json.dumps(plan_ir, indent=2),
            topology_json=json.dumps(self.topology, indent=2)
        )

        return prompt

    def build_refinement_prompt(self, context):
        """
        Builds prompt for configuration repair based on verifier findings.
        """
        # Get the last generated configuration that failed verification.
        current_config_bundle = self._extract_latest_success_data(context)
        # Get the error report from the verifier.
        verification_report = self._extract_latest_verification_report(context)

        # Inject the failed config and the error report into the repair template.
        prompt = self.repair_template.format(
            device_config_bundle_json=json.dumps(current_config_bundle, indent=2),
            verification_report_json=json.dumps(verification_report, indent=2),
            topology_json=json.dumps(self.topology, indent=2)
        )

        return prompt

    # ==========================================================
    # PRIVATE HELPERS
    # ==========================================================

    def _load_text(self, filename: str) -> str:
        """Helper function to read a text file from the templates directory."""
        file_path = self.templates_path / filename
        return file_path.read_text(encoding="utf-8")

    def _load_json(self, filename: str) -> dict:
        """Helper function to read and parse a JSON file from the templates directory."""
        file_path = self.templates_path / filename
        return json.loads(file_path.read_text(encoding="utf-8"))

    def _extract_latest_success_data(self, context):
        """
        Finds the 'data' field from the most recent successful iteration in the context history.

        It searches backwards through the iterations to find the last time the model
        returned a response with "status": "success". This is crucial for chaining
        prompts, as each step depends on the successful output of the previous one.
        """
        # Search in reverse to find the most recent successful response.
        for iteration in reversed(context.iterations):
            response = iteration.get("response", {})
            if response.get("status") == "success":
                # Return the data payload of that successful response.
                return response.get("data")

        # Return None if no successful iteration is found.
        return None

    def _extract_latest_verification_report(self, context):
        """
        Extracts last verifier response.
        """

        # Search in reverse to find the most recent verification attempt.
        for iteration in reversed(context.iterations):
            # Verification calls are identified by a specific prompt string.
            if iteration.get("prompt") == "VERIFICATION_CALL":
                response = iteration.get("response", {})
                return response.get("data", {}) # Return an empty dict if data is not found

        # If no verification call is found, return an empty dict to prevent errors.
        return {} # Return an empty dict if no verification call is found
