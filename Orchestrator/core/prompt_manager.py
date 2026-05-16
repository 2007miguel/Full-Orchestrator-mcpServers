import json
from pathlib import Path

class PromptManager:
    """
    Acts as a factory for creating specific prompts for each step of the orchestration pipeline.
    It loads templates and static data at initialization to be efficient.
    """

    def __init__(self, templates_path: str):
        """
        Initializes the PromptManager by loading all necessary templates and static data.
        """
        self.templates_path = Path(templates_path)

        # Pre-load all templates from disk to avoid repeated I/O on each call.
        self.classifier_promt_template = self._load_text("classifier_promt.txt")
        self.tasks_prompt_template = self._load_text("tasks_prompt.txt")
        self.config_prompt_template = self._load_text("config_prompt.txt")
        self.repair_template = self._load_text("post_verification_prompt.txt")

        # Load static base topology from config.txt as raw text
        self.topology_text = self._load_text("context_topology.txt")

        # Load the full base configuration snapshot for merging before verification.
        self.base_config_text = self._load_text("config_base.txt")

    def _load_text(self, filename: str) -> str:
        return (self.templates_path / filename).read_text(encoding="utf-8")

    def _load_json(self, filename: str) -> dict:
        content = self._load_text(filename)
        return json.loads(content)

    # ==========================================================
    # PUBLIC METHODS (Used by ExecutionController)
    # ==========================================================

    def build_classifier_prompt(self, context):
        """
        Builds prompt for classification.
        This is the first step in the pipeline.
        """
        user_text = context.intent

        # Inserta los datos dinámicos en la plantilla.
        prompt = self.classifier_promt_template.format(
            user_text=user_text,
            topology=self.topology_text
        )

        return prompt

    def get_base_config_text(self) -> str:
        """Returns the pre-loaded base configuration snapshot content as a string."""
        return self.base_config_text

    def build_tasks_prompt(self, context):
        """
        Builds prompt for high-level task decomposition.
        Requires previous classification output stored in context.
        """
        user_text = context.intent

        classification_data_str = self._extract_latest_success_data(context)

        prompt = self.tasks_prompt_template.format(
            user_text=user_text,
            topology=self.topology_text,
            classification=classification_data_str if classification_data_str else "{}"
        )

        return prompt

    def build_config_prompt(self, context):
        """
        Builds prompt for configuration generation.
        Requires previous tasks output stored in context.
        """
        plan_ir_str = self._extract_latest_success_data(context)
        
        prompt = self.config_prompt_template.format(
            context_plan=plan_ir_str if plan_ir_str else "{}",
            topology=self.topology_text
        )

        return prompt 
    
    def build_config_prompt_v2(self, context):
        """
        Builds prompt for configuration generation.
        """
        user_text = context.intent

        # Inserta los datos dinámicos en la plantilla.
        prompt = self.config_prompt_template.format(
            context_plan=user_text,
            topology=self.topology_text
        )

        return prompt

    def build_refinement_prompt(self, context):
        """
        Builds prompt for configuration repair based on verifier findings.
        """
        import json
        current_config = self._extract_latest_success_data(context)
        verification_report = self._extract_latest_verification_report(context)

        prompt = self.repair_template.format(
            device_config=current_config if current_config else "{}",
            verification_report=json.dumps(verification_report, indent=2) if verification_report else "{}",
            topology=self.topology_text
        )
        return prompt

    # ==========================================================
    # PRIVATE HELPER METHODS
    # ==========================================================

    def _extract_latest_success_data(self, context) -> str:
        """
        Finds the 'text' or 'data' field from the most recent successful iteration in the context.
        Ignores verification attempts as they don't produce model text output.
        """
        for iteration in reversed(context.iterations):
            if iteration.get("prompt") == "VERIFICATION_CALL":
                continue
                
            response = iteration.get("response", {})
            
            is_success = response.get("isError") is False or response.get("status") == "success"
            if not is_success:
                continue
                
            content = response.get("content", [])
            if content and isinstance(content, list) and len(content) > 0:
                raw_text = content[0].get("text", "")
                if isinstance(raw_text, str):
                    # Parsear literales del LLM para que los saltos de línea se procesen bien en plantillas
                    return raw_text.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
                return raw_text
            else:
                return response.get("data", "")
        return ""

    def _extract_latest_verification_report(self, context) -> dict:
        """
        Finds the 'data' from the most recent verification call.
        """
        for iteration in reversed(context.iterations):
            if iteration.get("prompt") == "VERIFICATION_CALL":
                response = iteration.get("response", {})
                return response.get("data", {})
        return {}
