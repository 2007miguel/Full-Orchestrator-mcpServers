import json
from pathlib import Path
from mcp_client import MCPClientManager, SERVERS
from utils.create_snapshot import create_snapshot_from_config
# --- 1. Crear el snapshot dinámicamente ---
print("--- Creando snapshot ---")
# Definir rutas relativas al script actual (test.py)
script_dir = Path(__file__).resolve().parent
config_path = script_dir / "templates" / "config.txt"
snapshot_base_dir = Path.home() / "Documents" / "snapshot"

# Crear el archivo zip y obtener su ruta
zip_file_path = create_snapshot_from_config(str(config_path), base_dir=str(snapshot_base_dir))

if not zip_file_path:
    print("Error: No se pudo crear el archivo snapshot.zip. Abortando prueba.")
    exit()

print(f"Snapshot creado en: {zip_file_path}\n")

# --- 2. Inicializar el cliente y probar el servidor batfish ---
client_manager = MCPClientManager(SERVERS)

try:
    # --- 2.1 Probar el servidor batfish ---
    print("--- Probando servidor Batfish ---")
    batfish_server = client_manager.server("batfish")
    batfish_server.start()

    init_response_batfish = batfish_server.initialize()
    print("Respuesta de inicialización de Batfish:")
    print(json.dumps(init_response_batfish, indent=2, ensure_ascii=False))

    print("\n=== load_snapshot ===")
    load_response = batfish_server.call_tool("load_snapshot", {"zip_path": zip_file_path})
    print(json.dumps(load_response, indent=2, ensure_ascii=False))

    print("\n=== file_parse_status ===")
    status_response = batfish_server.call_tool("file_parse_status", {})
    print(json.dumps(status_response, indent=2, ensure_ascii=False)) 

    print("\n=== parse_warning ===")
    warning_response = batfish_server.call_tool(
        "parse_warning",
        {"aggregate_duplicates": True}
    )
    print(json.dumps(warning_response, indent=2, ensure_ascii=False))
    print("\n=== init_issues ===")
    issues_response = batfish_server.call_tool("init_issues", {})
    print(json.dumps(issues_response, indent=2, ensure_ascii=False))

    # --- 2.2 Probar el servidor flm ---
    print("\n\n--- Probando servidor FLM ---")
    flm_server = client_manager.server("flm")
    flm_server.start()

    init_response_flm = flm_server.initialize()
    print("Respuesta de inicialización de FLM:")
    print(json.dumps(init_response_flm, indent=2, ensure_ascii=False))

    print("\n=== Llamando a send_prompt 3 veces ===")
    for i in range(1, 4):
        prompt_text = f"random {i}"
        print(f"\n--- Llamada {i} con prompt: '{prompt_text}' ---")
        flm_response = flm_server.call_tool(
            "send_prompt",
            {"prompt": prompt_text}
        )
        print(json.dumps(flm_response, indent=2, ensure_ascii=False))

finally:
    print("\n--- Cerrando todos los servidores ---")
    client_manager.close_all()