import json
from pathlib import Path
from mcp_client import start, initialize, call_tool, close
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

start()
try:
    init_response = initialize()

    print("\n=== load_snapshot ===")
    load_response = call_tool("load_snapshot", {"zip_path": zip_file_path})
    print(json.dumps(load_response, indent=2, ensure_ascii=False))

    print("\n=== file_parse_status ===")
    status_response = call_tool("file_parse_status", {})
    print(json.dumps(status_response, indent=2, ensure_ascii=False)) 
    
    print("\n=== parse_warning ===")
    warning_response = call_tool(
        "parse_warning",
        {
            "aggregate_duplicates": True
        }
    )
    print(json.dumps(warning_response, indent=2, ensure_ascii=False))

    print("\n=== init_issues ===")
    issues_response = call_tool("init_issues", {})
    print(json.dumps(issues_response, indent=2, ensure_ascii=False))
finally:
    close()