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
        self.intent_normalization_template = self._load_text("intent_normalization_prompt.txt")
        self.config_rag_prompt_template = self._load_text("config_prompt_rag.txt")
        self.config_no_rag_prompt_template = self._load_text("config_prompt_no_rag.txt")
        self.repair_template = self._load_text("post_verification_prompt.txt")
        self.line_fix_template = self._load_text("line_fix_prompt.txt")

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

    def build_intent_normalization_prompt(self, context, arch_base: dict = None) -> str:
        """
        Builds the first RAG prompt: enrich the user requirement into a single
        retrieval query, matching the v17 pipeline (INTENT_NORMALIZATION_PROMPT).
        The v17 prompt is architecture-agnostic, so arch_base is accepted for
        backward compatibility but no longer injected.
        """
        return self.intent_normalization_template.format(
            requirement=context.intent,
        )

    def build_config_prompt_rag(self, context, retrieved_context: str) -> str:
        """
        Builds the generation prompt using the topology, user requirement,
        and the retrieved Cisco documentation returned by Colab.
        """
        return self.config_rag_prompt_template.format(
            topology=self.topology_text,
            retrieved_context=retrieved_context or "No relevant Cisco documentation was retrieved.",
            requirement=context.intent,
        )

    def build_config_prompt_no_rag(self, context) -> str:
        """
        Builds the generation prompt WITHOUT retrieved documentation, matching
        the v17 pipeline (GENERATION_PROMPT_NO_RAG). Used when retrieval returns
        no chunks, so the model is not fed a "No relevant documentation" block.
        """
        return self.config_no_rag_prompt_template.format(
            topology=self.topology_text,
            requirement=context.intent,
        )

    def build_line_fix_prompt(self, context, invalid_items: list[dict]) -> str:
        """
        Builds a minimal repair prompt that asks the model to correct ONLY the
        specific line(s) Batfish flagged, one per invalid line, in order.
        The whole-config reproduction is handled deterministically in code.
        """
        lines_block = "\n".join(
            f"{index}. {item['full_line']}\n"
            f"   Parser context: {item['parser_context']}\n"
            f"   Reason: {item['reason']}"
            for index, item in enumerate(invalid_items, start=1)
        )
        return self.line_fix_template.format(
            requirement=context.intent,
            invalid_lines=lines_block,
        )

    def build_refinement_prompt(self, context):
        """
        Builds prompt for configuration repair based on verifier findings.
        """
        import json
        current_config = self._extract_latest_success_data(context)
        verification_report = self._extract_latest_verification_report(context)

        prompt = self.repair_template.format(
            requirement=context.intent,
            device_config=current_config if current_config else "{}",
            verification_report=json.dumps(verification_report, indent=2) if verification_report else "{}",
            retrieved_context=self._build_repair_retrieved_context(context),
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

    def _build_repair_retrieved_context(self, context) -> str:
        chunks = getattr(context, "retrieved_chunks", []) or []
        if chunks:
            blocks = []
            for index, chunk in enumerate(chunks[:2], start=1):
                commands = self._clip_text(str(chunk.get("commands", "")), 700)
                examples = self._clip_text(str(chunk.get("examples", "")), 700)
                blocks.append("\n".join([
                    f"[RETRIEVED CHUNK {index}]",
                    f"Device type: {chunk.get('device_type', '')}",
                    f"Product: {chunk.get('product', '')}",
                    f"Version: {chunk.get('version', '')}",
                    f"Section: {chunk.get('section', '')}",
                    "Commands:",
                    commands,
                    "Examples:",
                    examples,
                ]).strip())
            return "\n\n".join(blocks)

        retrieved_context = getattr(context, "retrieved_context", "") or ""
        return self._clip_text(retrieved_context, 1800) if retrieved_context else "No relevant Cisco documentation was retrieved."

    @staticmethod
    def _clip_text(text: str, limit: int) -> str:
        text = " ".join(str(text or "").split())
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0] + " ..."
