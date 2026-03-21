import json
import subprocess
from pathlib import Path
from typing import Any, Optional


# =========================
# Configuración del servidor
# =========================
BASE_PATH = Path("C:/Users/juan/Documents/Miguel/tesis/System/Orchestrator-mcpServers")
PYTHON_EXE = BASE_PATH / "mcp-server-batfish/venv/Scripts/python.exe"
SERVER_SCRIPT = BASE_PATH / "mcp-server-batfish/server.py"

DEFAULT_COMMAND = [str(PYTHON_EXE), str(SERVER_SCRIPT)]
PROTOCOL_VERSION = "2025-03-26"


# =========================
# Estado interno
# =========================
_process: Optional[subprocess.Popen[str]] = None
_next_id = 1
_initialized = False


# =========================
# Utilidades internas
# =========================
def _require_process() -> subprocess.Popen[str]:
    if _process is None:
        raise RuntimeError("El cliente MCP no ha sido iniciado. Llama primero a start().")
    return _process


def _send_message(message: dict[str, Any]) -> None:
    process = _require_process()

    if process.stdin is None:
        raise RuntimeError("stdin del proceso no está disponible.")

    process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    process.stdin.flush()


def _read_message() -> dict[str, Any]:
    process = _require_process()

    if process.stdout is None:
        raise RuntimeError("stdout del proceso no está disponible.")

    while True:
        line = process.stdout.readline()

        if line == "":
            raise RuntimeError("El servidor cerró stdout o no respondió.")

        line = line.strip()
        if not line:
            continue

        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"El servidor devolvió JSON inválido: {line}") from e


def _ensure_initialized() -> None:
    if not _initialized:
        raise RuntimeError("El cliente MCP no ha sido inicializado. Llama primero a initialize().")


# =========================
# API pública
# =========================
def start(command: Optional[list[str]] = None) -> None:
    """
    Inicia el servidor MCP como subproceso.
    No hace initialize automáticamente.
    """
    global _process, _next_id, _initialized

    if _process is not None and _process.poll() is None:
        return

    _process = subprocess.Popen(
        command or DEFAULT_COMMAND,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,   # deja los logs del server visibles en consola
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    _next_id = 1
    _initialized = False


def send_request(method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """
    Envía una request JSON-RPC y devuelve el campo result.
    Este cliente simple asume una sola request a la vez.
    """
    global _next_id

    _require_process()

    request_id = _next_id
    _next_id += 1

    message = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }

    _send_message(message)

    while True:
        response = _read_message()

        # Notificación del servidor: se ignora en este cliente simple
        if "method" in response and "id" not in response:
            continue

        # Request del servidor hacia el cliente: no soportada aquí
        if "method" in response and "id" in response:
            raise RuntimeError(
                f"El servidor envió una request al cliente y este cliente simple no la soporta: {response}"
            )

        # Respuesta JSON-RPC
        if response.get("id") != request_id:
            raise RuntimeError(
                f"Se recibió una respuesta con id inesperado. Esperado={request_id}, recibido={response.get('id')}"
            )

        if "error" in response:
            raise RuntimeError(f"Error MCP: {response['error']}")

        if "result" not in response:
            raise RuntimeError(f"Respuesta MCP inválida: {response}")

        return response["result"]


def send_notification(method: str, params: Optional[dict[str, Any]] = None) -> None:
    """
    Envía una notificación JSON-RPC.
    """
    _require_process()

    message = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
    }

    _send_message(message)


def initialize() -> dict[str, Any]:
    """
    Realiza el handshake MCP:
    1) initialize
    2) notifications/initialized
    """
    global _initialized

    result = send_request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "simple-client",
                "version": "1.0.0",
            },
        },
    )

    send_notification("notifications/initialized", {})
    _initialized = True
    return result


def list_tools() -> dict[str, Any]:
    """
    Solicita tools/list al servidor.
    """
    _ensure_initialized()
    return send_request("tools/list", {})


def call_tool(name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """
    Ejecuta tools/call.
    """
    _ensure_initialized()
    return send_request(
        "tools/call",
        {
            "name": name,
            "arguments": arguments or {},
        },
    )


def close() -> None:
    """
    Cierra el proceso del servidor.
    """
    global _process, _initialized

    if _process is None:
        return

    try:
        if _process.stdin:
            _process.stdin.close()
    except Exception:
        pass

    try:
        _process.terminate()
        _process.wait(timeout=3)
    except Exception:
        try:
            _process.kill()
        except Exception:
            pass

    _process = None
    _initialized = False


if __name__ == "__main__":
    start()
    try:
        print("INIT:")
        print(json.dumps(initialize(), indent=2, ensure_ascii=False))

        print("\nTOOLS:")
        print(json.dumps(list_tools(), indent=2, ensure_ascii=False))

        # Ejemplo:
        # print("\nCALL:")
        # print(json.dumps(call_tool("nombre_tool", {"x": 1}), indent=2, ensure_ascii=False))

    finally:
        close()