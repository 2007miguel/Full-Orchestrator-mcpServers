import json
from pathlib import Path

# Import core components of the orchestration system
from core.execution_controller import ExecutionController
from core.prompt_manager import PromptManager
from core.requirement_loader import RequirementLoader
from models.execution_context import ExecutionContext

# --- Mock/Dummy Implementations for Demonstration ---
# In a real-world scenario, these would be replaced by actual clients and loggers.

class DummyMCPClient:
    """
    A mock Multi-Component Protocol (MCP) client that simulates calls to external tools.
    It returns predefined successful responses to demonstrate the full pipeline.
    """
    def call_tool(self, tool_name: str, payload: dict) -> dict:
        print(f"---  simulating call to tool: '{tool_name}' ---")

        if tool_name == "flm":
            # Simulate a successful response from the Foundational Language Model
            return {
                "status": "success",
                "data": {
                    "message": f"Simulated successful output from {tool_name}",
                    "tool_payload": payload
                }
            }
        elif tool_name == "verifier":
            # Simulate a successful response from the network verifier
            return {
                "status": "success",
                "data": {
                    "is_compliant": True,
                    "report": "Configuration is compliant and meets all checks."
                }
            }
        
        # Default failure response for unknown tools
        return {
            "status": "error",
            "data": {"message": f"Tool '{tool_name}' not found."}
        }

class ConsoleResultLogger:
    """
    A simple logger that prints the final execution context to the console.
    """
    def save(self, context: ExecutionContext):
        print("\n--- Execution Finished. Saving result to console. ---")
        # Use the context's own serialization method for a clean output
        print(json.dumps(context.to_dict(), indent=2))
        print("-----------------------------------------------------\n")

def main():
    """
    Main function to set up the orchestrator and run the conversational CLI.
    """
    print("Orchestrator CLI Initialized.")

    # --- Dependency Injection Setup ---

    # 1. Define the path to the prompt templates
    # Assuming 'main.py' is in the root and templates are in 'data/templates'
    templates_path = Path(__file__).parent / "templates"

    # 2. Instantiate all the necessary components
    requirement_loader = RequirementLoader()
    prompt_manager = PromptManager(templates_path=str(templates_path))
    mcp_client = MCPClient() # Usamos el cliente real
    result_logger = ConsoleResultLogger()

    # 3. Instantiate the main controller with all its dependencies
    controller = ExecutionController(
        prompt_manager=prompt_manager,
        mcp_client=mcp_client,
        result_logger=result_logger
    )

    # --- Test MCP connection by listing available tools ---
    try:
        mcp_client.call_tool("tools/list")
    except Exception as e:
        print(f"Failed to connect to MCP Server. Please check server implementation. Error: {e}")
 
    print("Topology Summary: ")
    print("""H1 → R1
R1 → R2, R4
R2 ↔ R4 ↔ R3
R3 → H2""")
    print("Enter your network requirement. Type 'exit' or 'quit' to stop.")

    # --- Conversational Loop ---

    while True:
        try:
            user_input = input("\n> ")

            if user_input.lower() in ["exit", "quit"]:
                print("Exiting orchestrator.")
                mcp_client.close() # Cierra el proceso del servidor al salir
                break

            if not user_input.strip():
                continue

            # 1. Load and normalize the user's requirement
            requirement = requirement_loader.load(user_input)
            print(f"--- Requirement loaded (ID: {requirement['request_id']}) ---")

            # 2. Run the main orchestration pipeline
            final_context = controller.run(requirement)

            # 3. Provide feedback to the user based on the final state
            if final_context.is_success():
                print(f" Requirement processed successfully!")
            else:
                print(f" Execution failed. State: {final_context.state.value}")
                if final_context.error_message:
                    print(f"   Error: {final_context.error_message}")

        except ValueError as e:
            print(f"Error: {e}")
        except KeyboardInterrupt:
            print("\nExiting orchestrator by user interrupt.")
            mcp_client.close() # Asegúrate de cerrar también con Ctrl+C
            break
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            mcp_client.close() # Cierra en caso de un error inesperado


if __name__ == "__main__":
    # Ensure you have a folder structure like:
    # /Orchestrator
    #   - main.py
    #   - /core
    #   - /models
    #   - /templates/*.txt
    main()