import json
import sys
from pathlib import Path

# Import core components of the orchestration system
from core.execution_controller import ExecutionController
from core.prompt_manager import PromptManager
from core.requirement_loader import RequirementLoader
from models.execution_context import ExecutionContext

# Import the real MCP Client Manager
from mcp_client import MCPClientManager, SERVERS

class ConsoleResultLogger:
    """
    A simple logger that prints the final execution context to the console
    and saves it to a file.
    """
    def save(self, context: ExecutionContext):
        print("\n--- Execution Finished. Saving result ---")
        
        # Guardar en archivo
        results_dir = Path(__file__).parent / "results"
        results_dir.mkdir(exist_ok=True)
        filename = results_dir / f"execution_{context.metadata.get('request_id', 'unknown')}.json"
        
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(context.to_dict(), f, indent=2, ensure_ascii=False)
            
        print(f"Resultado guardado en: {filename}")
        print("-----------------------------------------------------\n")


def main():
    """
    Main function to set up the orchestrator and run the conversational CLI.
    """
    print("Orchestrator CLI Initialized.")

    # --- Dependency Injection Setup ---

    # 1. Define the path to the prompt templates
    templates_path = Path(__file__).parent / "templates"

    # 2. Instantiate all the necessary components
    requirement_loader = RequirementLoader()
    prompt_manager = PromptManager(templates_path=str(templates_path))
    
    # 3. Setup MCP Client Manager
    print("Iniciando servidores MCP...")
    mcp_client_manager = MCPClientManager(SERVERS)
    
    try:
        # Iniciar servidor Batfish
        batfish_server = mcp_client_manager.server("batfish")
        batfish_server.start()
        batfish_server.initialize()
        print("- Servidor Batfish iniciado correctamente.")

        # Iniciar servidor FLM
        flm_server = mcp_client_manager.server("flm")
        flm_server.start()
        flm_server.initialize()
        print("- Servidor FLM iniciado correctamente.")

    except Exception as e:
        print(f"Error al inicializar servidores MCP: {e}")
        mcp_client_manager.close_all()
        sys.exit(1)

    result_logger = ConsoleResultLogger()

    # 4. Instantiate the main controller with all its dependencies
    controller = ExecutionController(
        prompt_manager=prompt_manager,
        mcp_client=mcp_client_manager,
        result_logger=result_logger
    )

    print("\nTopology Summary: ")
    print("""H1 → R1
R1 → R2, R4
R2 ↔ R4 ↔ R3
R3 → H2""")
    print("\nIngresa tu requerimiento de red. Escribe 'exit' o 'quit' para salir.")

    # --- Conversational Loop ---
    try:
        while True:
            user_input = input("\n> ")

            if user_input.lower() in ["exit", "quit"]:
                print("Exiting orchestrator.")
                break

            if not user_input.strip():
                continue

            # 1. Load and normalize the user's requirement
            requirement = requirement_loader.load(user_input)
            print(f"--- Requirement loaded (ID: {requirement['request_id']}) ---")

            # 2. Run the main orchestration pipeline
            print("Ejecutando pipeline de orquestación...")
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
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        print("Cerrando servidores MCP...")
        mcp_client_manager.close_all()


if __name__ == "__main__":
    main()