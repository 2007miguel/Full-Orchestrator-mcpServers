import json
from pathlib import Path

from core.prompt_manager import PromptManager
from models.execution_context import ExecutionContext
from mcp_client import MCPClientManager, SERVERS
from utils.create_snapshot import create_snapshot_from_string

def test_workflow():
    print("--- Inicializando dependencias ---")
    pm = PromptManager("templates")
    client_manager = MCPClientManager(SERVERS)
    
    try:
        # 1. Iniciar servidor FLM
        flm_server = client_manager.server("flm")
        flm_server.start()
        flm_server.initialize()
        print("FLM Server inicializado correctamente.")

        # 2. Crear un requisito
        req = {
            "intent": "Configurar una ruta estática en el router R1 hacia la red 192.168.1.0/24 usando el next-hop 10.0.0.2"
        }
        context = ExecutionContext(req)

        # ----------------------------------------------------
        # 1a. Classification
        # ----------------------------------------------------
        print("\n\n" + "="*50)
        print("1a. CLASSIFICATION")
        print("="*50)
        
        classifier_prompt = pm.build_classifier_prompt(context)
        print("\n[PROMPT ENVIADO]:")
        print(classifier_prompt)
        
        classifier_response = flm_server.call_tool("send_prompt", {"prompt": classifier_prompt})
        context.update(classifier_prompt, classifier_response)

        print("\n[RESPUESTA RECIBIDA (RAW)]:")
        print(json.dumps(classifier_response, indent=2, ensure_ascii=False))
        if classifier_response.get("isError"):
            print("ERROR FATAL en 1a. Abortando.")
            return

        # ----------------------------------------------------
        # 1b. Step Generation
        # ----------------------------------------------------
        print("\n\n" + "="*50)
        print("1b. STEP GENERATION")
        print("="*50)

        tasks_prompt = pm.build_tasks_prompt(context)
        print("\n[PROMPT ENVIADO]:")
        print(tasks_prompt)
        
        tasks_response = flm_server.call_tool("send_prompt", {"prompt": tasks_prompt})
        context.update(tasks_prompt, tasks_response)

        print("\n[RESPUESTA RECIBIDA (RAW)]:")
        print(json.dumps(tasks_response, indent=2, ensure_ascii=False))
        if tasks_response.get("isError"):
            print("ERROR FATAL en 1b. Abortando.")
            return

        # ----------------------------------------------------
        # 2. Configuration Generation 
        # ----------------------------------------------------
        print("\n\n" + "="*50)
        print("2. CONFIGURATION GENERATION")
        print("="*50)

        config_prompt = pm.build_config_prompt(context)
        print("\n[PROMPT ENVIADO]:")
        print(config_prompt)
        
        config_response = flm_server.call_tool("send_prompt", {"prompt": config_prompt})
        context.update(config_prompt, config_response)

        print("\n[RESPUESTA RECIBIDA (RAW)]:")
        print(json.dumps(config_response, indent=2, ensure_ascii=False))
        
        if config_response.get("isError"):
            print("ERROR FATAL en 2. Abortando.")
            return

        # Simular empaquetado del resultado del contexto
        print("\n\n" + "="*50)
        print("SIMULANDO CREACION DEL SNAPSHOT (Regex Zip)")
        print("="*50)
        final_text = context.final_result
        if final_text:
            print("El texto en memoria a parsear es (truncado):", final_text[:100], "...")
            # Aquí iría el zip parsing del regex si tuviera formato correcto "[filename] ..."
        else:
            print("No se extrajo final_result válido.")

    finally:
        print("\n--- Cerrando servidor ---")
        client_manager.close_all()

if __name__ == "__main__":
    test_workflow()
