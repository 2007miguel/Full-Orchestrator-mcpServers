import json
from pathlib import Path
import utils.create_snapshot

from core.prompt_manager import PromptManager
from core.execution_controller import ExecutionController
from models.execution_context import ExecutionContext
from mcp_client import MCPClientManager, SERVERS

class DummyLogger:
    def save(self, context):
        print("\n[LOGGER] Ejecución guardada. Estado final:", context.state.name)

def test_workflow_loop():
    print("--- Inicializando dependencias ---")
    pm = PromptManager("templates")
    client_manager = MCPClientManager(SERVERS)
    
    try:
        # Iniciar servidores
        flm_server = client_manager.server("flm")
        flm_server.start()
        flm_server.initialize()
        
        batfish_server = client_manager.server("batfish")
        batfish_server.start()
        batfish_server.initialize()

        print("Servidores inicializados correctamente.")

        # ==========================================================
        # INTERCEPTOR FLM: Permite observar cada prompt y respuesta
        # ==========================================================
        original_flm_call = flm_server.call_tool
        def debug_flm_call(tool_name, payload):
            print("\n\n" + "="*80)
            print(f"🚀 ENVIANDO A FLM SERVER (Tool: {tool_name})")
            print("="*80)
            print(payload.get("prompt", json.dumps(payload)))
            
            resp = original_flm_call(tool_name, payload)
            
            print("\n" + "-"*80)
            print("📥 RESPUESTA DE FLM SERVER (RAW)")
            print("-"*80)
            print(json.dumps(resp, indent=2, ensure_ascii=False))
            return resp
        
        flm_server.call_tool = debug_flm_call

        # ==========================================================
        # INTERCEPTOR BATFISH: Para observar la validación en el loop
        # ==========================================================
        original_bf_call = batfish_server.call_tool
        def debug_bf_call(tool_name, payload):
            print("\n" + "~"*80)
            print(f"🐟 LLAMADA A BATFISH: {tool_name}")
            print("~"*80)
            if payload:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                
            resp = original_bf_call(tool_name, payload)
            
            print("\n" + "-"*80)
            print(f"📥 RESPUESTA DE BATFISH (RAW) - Tool: {tool_name}")
            print("-" * 80)
            print(json.dumps(resp, indent=2, ensure_ascii=False))
            return resp
            
        batfish_server.call_tool = debug_bf_call

        # ==========================================================
        # INTERCEPTOR SNAPSHOT: Para verificar el merge antes de crear el zip
        # ==========================================================
        original_create_snapshot = utils.create_snapshot.create_snapshot_from_string
        def debug_create_snapshot(content, base_dir):
            print("\n" + "#"*80)
            print("📦 CREANDO SNAPSHOT CON EL SIGUIENTE CONTENIDO (MERGED)")
            print("#"*80)
            print(content)
            print("#"*80)
            
            zip_path = original_create_snapshot(content, base_dir)
            return zip_path
        utils.create_snapshot.create_snapshot_from_string = debug_create_snapshot

        # Configurar Orchestrator para usar nuestro cliente interceptado
        controller = ExecutionController(
            prompt_manager=pm,
            mcp_client=client_manager,
            result_logger=DummyLogger(),
            max_refinement_iterations=3
        )

        # Requisito de prueba
        req = {
            "intent": "Configurar una ruta estática en el router R1 hacia la red 192.168.1.0/24 usando el next-hop 10.0.0.2"
        }

        print("\n\n" + ">>> "*5 + "INICIANDO ORCHESTRATOR RUN" + " <<<"*5)
        # Esto disparará Classification -> Step Gen -> Config Gen -> Verification -> (Refinement Loop si falla)
        final_context = controller.run(req)

        print("\n\n" + "*"*80)
        print("RESULTADO FINAL DEL CONTEXTO")
        print("*"*80)
        print("ESTADO FINAL:", final_context.state.name)
        
        if final_context.state.name != "SUCCESS":
            print("❌ LA VERIFICACIÓN FALLÓ A PESAR DEL REFINEMENT LOOP O NO SE LOGRÓ COMPLETAR.")
        else:
            print("✅ CONFIGURACIÓN EXITOSA Y VALIDADA POR BATFISH DIRECTAMENTE DESDE EL CÓDIGO PRODUCTIVO.")

    finally:
        print("\n--- Cerrando servidores ---")
        client_manager.close_all()

if __name__ == "__main__":
    test_workflow_loop()
