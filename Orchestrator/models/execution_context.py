import uuid
from datetime import datetime
from models.state import ExecutionState

class ExecutionContext:
    """Manages the state and lifecycle of a single task execution."""

    def __init__(self, requirement: dict):
        """
        Initializes the execution context for a new requirement.
        """
        self.id = str(uuid.uuid4())
        self.created_at = datetime.utcnow().isoformat()

        self.intent = requirement.get("intent")
        self.metadata = requirement

        self.iterations = []
        self.current_iteration = 0

        self.state = ExecutionState.RUNNING
        self.final_result = None
        self.error_message = None

    def update(self, prompt: str, response: dict):
        """
        Records a new iteration and updates the execution state based on the response.
        """
        self.current_iteration += 1

        iteration_record = {
            "iteration": self.current_iteration,
            "prompt": prompt,
            "response": response,
            "timestamp": datetime.utcnow().isoformat()
        }

        self.iterations.append(iteration_record)

        if response.get("status") == "success":
            # Store the latest successful data, but don't mark the whole context as success yet.
            # The controller decides when the final success state is reached.
            self.final_result = response.get("data")

    def is_success(self) -> bool:
        """
        Checks if the execution has completed successfully.
        """
        return self.state == ExecutionState.SUCCESS 

    def mark_success(self):
        """Marks the execution as successfully completed."""
        self.state = ExecutionState.SUCCESS
    
    def mark_failed(self, message: str):
        """
        Marks the execution as failed and records an error message.
        """
        self.state = ExecutionState.FAILED
        self.error_message = message

    def to_dict(self):
        """
        Serializes the execution context to a dictionary.
        """
        return {
            "id": self.id,
            "created_at": self.created_at,
            "intent": self.intent,
            "state": self.state.value,
            "iterations": self.iterations,
            "final_result": self.final_result,
            "error_message": self.error_message
        }
