from enum import Enum

class ExecutionState(Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"
