import uuid
from datetime import datetime


class RequirementLoader:
    """
    Responsible for normalizing and structuring raw user input
    into a standardized requirement dictionary.

    This module does NOT perform validation against tools,
    does NOT create ExecutionContext,
    and does NOT contain orchestration logic.
    """

    def load(self, user_text: str) -> dict:
        """
        Transforms raw user input into structured requirement data.

        Returns:
            dict: Structured requirement compatible with ExecutionContext.
        """

        if not isinstance(user_text, str) or not user_text.strip():
            raise ValueError("User input must be a non-empty string.")

        requirement = {
            "request_id": str(uuid.uuid4()),
            "timestamp": datetime.utcnow().isoformat(),
            "intent": user_text.strip(),
            "source": "user_input"
        }

        return requirement
