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

        # Verificación con Batfish
        print("\n\n" + "="*50)
        print("3. VERIFICANDO CONFIGURACIÓN (Batfish)")
        print("="*50)
        config_data = context.final_result
        
        if config_data:
            # Corrección de escapes literales (doble escape de string)
            if config_data.startswith('"') and config_data.endswith('"'):
                try:
                    config_data = json.loads(config_data)
                except Exception:
                    pass
            config_data = config_data.replace("\\n", "\n").replace('\\"', '"').replace("\\t", "\t")
            
            import os
            snapshot_dir = os.path.join(os.path.expanduser("~"), "snapshot_verify_temp")
            zip_path = create_snapshot_from_string(config_data, base_dir=snapshot_dir)
            
            if not zip_path:
                print("Error: No se pudo crear el zip temporal con regex.")
                return

            print("Snapshot ZIP creado exitosamente en:", zip_path)
            
            batfish_server = client_manager.server("batfish")
            batfish_server.start()
            print("Batfish Server inicializado:", batfish_server.initialize())
            
            print("\nEnviando load_snapshot a Batfish...")
            load_response = batfish_server.call_tool("load_snapshot", {"zip_path": zip_path})
            print(json.dumps(load_response, indent=2, ensure_ascii=False))

            print("\nEnviando init_issues a Batfish...")
            issues_response = batfish_server.call_tool("init_issues", {})
            print(json.dumps(issues_response, indent=2, ensure_ascii=False))
            
            has_errors = issues_response.get("isError", False)
            if has_errors:
                print("\nBatfish reportó un ERROR de inicialización/validación.")
            else:
                print("\nVerificación de red EXITOSA.")
                
        else:
            print("No se extrajo final_result válido.")

    finally:
        print("\n--- Cerrando servidor ---")
        client_manager.close_all()

if __name__ == "__main__":
    test_workflow()
