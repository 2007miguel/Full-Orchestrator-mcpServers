import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


PROTOCOL_VERSION = "2025-03-26"


# =========================
# Configuración de servidores
# =========================
BASE_PATH = Path("C:/Users/juan/Documents/Miguel/tesis/System/Orchestrator-mcpServers")

# servers añadidos
SERVERS: dict[str, list[str]] = {
    "batfish": [
        str(BASE_PATH / "mcp-server-batfish/venv/Scripts/python.exe"),
        str(BASE_PATH / "mcp-server-batfish/server.py"),
    ], 
    
    "flm": [
        str(BASE_PATH / "mcp-server-flm/venv/Scripts/python.exe"),
        str(BASE_PATH / "mcp-server-flm/server.py"),
    ],
    
    "csv": [
        str(BASE_PATH / "mcp-server-csv/venv/Scripts/python.exe"),
        str(BASE_PATH / "mcp-server-csv/server.py"),
    ]
}

@dataclass
class ServerConfig:
    name: str
    command: list[str]
    protocol_version: str = PROTOCOL_VERSION


class MCPServerSession:
    """
    Sesión MCP para un único servidor stdio.
    Cada servidor tiene su propio proceso, ids y estado de initialize.
    """

    def __init__(self, config: ServerConfig):
        self.config = config
        self._process: Optional[subprocess.Popen[str]] = None
        self._next_id = 1
        self._initialized = False

    # =========================
    # Utilidades internas
    # =========================
    def _require_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            raise RuntimeError(
                f"El servidor '{self.config.name}' no ha sido iniciado. Llama primero a start()."
            )
        return self._process

    def _send_message(self, message: dict[str, Any]) -> None:
        process = self._require_process()

        if process.stdin is None:
            raise RuntimeError(f"stdin del servidor '{self.config.name}' no está disponible.")

        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def _read_message(self) -> dict[str, Any]:
        process = self._require_process()

        if process.stdout is None:
            raise RuntimeError(f"stdout del servidor '{self.config.name}' no está disponible.")

        while True:
            line = process.stdout.readline()

            if line == "":
                raise RuntimeError(
                    f"El servidor '{self.config.name}' cerró stdout o no respondió."
                )

            line = line.strip()
            if not line:
                continue

            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"El servidor '{self.config.name}' devolvió JSON inválido: {line}"
                ) from e

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                f"El servidor '{self.config.name}' no ha sido inicializado. "
                f"Llama primero a initialize()."
            )

    # =========================
    # API pública
    # =========================
    def start(self) -> None:
        """
        Inicia el servidor MCP como subproceso.
        No hace initialize automáticamente.
        """
        if self._process is not None and self._process.poll() is None:
            return

        self._process = subprocess.Popen(
            self.config.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        self._next_id = 1
        self._initialized = False

    def send_request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """
        Envía una request JSON-RPC y devuelve el campo result.
        Este cliente simple asume una sola request a la vez por sesión.
        """
        self._require_process()

        request_id = self._next_id
        self._next_id += 1

        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }

        self._send_message(message)

        while True:
            response = self._read_message()

            # Notificación del servidor: se ignora
            if "method" in response and "id" not in response:
                continue

            # Request del servidor hacia el cliente: no soportada aquí
            if "method" in response and "id" in response:
                raise RuntimeError(
                    f"El servidor '{self.config.name}' envió una request al cliente "
                    f"y este cliente simple no la soporta: {response}"
                )

            # Respuesta JSON-RPC
            if response.get("id") != request_id:
                raise RuntimeError(
                    f"Se recibió una respuesta con id inesperado en '{self.config.name}'. "
                    f"Esperado={request_id}, recibido={response.get('id')}"
                )

            if "error" in response:
                raise RuntimeError(f"Error MCP en '{self.config.name}': {response['error']}")

            if "result" not in response:
                raise RuntimeError(
                    f"Respuesta MCP inválida en '{self.config.name}': {response}"
                )

            return response["result"]

    def send_notification(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None
    ) -> None:
        """
        Envía una notificación JSON-RPC.
        """
        self._require_process()

        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }

        self._send_message(message)

    def initialize(self) -> dict[str, Any]:
        """
        Realiza el handshake MCP:
        1) initialize
        2) notifications/initialized
        """
        result = self.send_request(
            "initialize",
            {
                "protocolVersion": self.config.protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": "simple-client",
                    "version": "1.0.0",
                },
            },
        )

        self.send_notification("notifications/initialized", {})
        self._initialized = True
        return result

    def list_tools(self) -> dict[str, Any]:
        self._ensure_initialized()
        return self.send_request("tools/list", {})

    def call_tool(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        self._ensure_initialized()
        return self.send_request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments or {},
            },
        )

    def close(self) -> None:
        """
        Cierra el proceso del servidor.
        """
        if self._process is None:
            return

        try:
            if self._process.stdin:
                self._process.stdin.close()
        except Exception:
            pass

        try:
            self._process.terminate()
            self._process.wait(timeout=3)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass

        self._process = None
        self._initialized = False


class MCPClientManager:
    """
    Administra múltiples servidores MCP por nombre.
    """

    def __init__(self, server_commands: dict[str, list[str]]):
        self._sessions: dict[str, MCPServerSession] = {
            name: MCPServerSession(ServerConfig(name=name, command=command))
            for name, command in server_commands.items()
        }

    def server(self, name: str) -> MCPServerSession:
        if name not in self._sessions:
            disponibles = ", ".join(sorted(self._sessions.keys())) or "ninguno"
            raise KeyError(
                f"Servidor '{name}' no configurado. Disponibles: {disponibles}"
            )
        return self._sessions[name]

    def add_server(self, name: str, command: list[str]) -> None:
        if name in self._sessions:
            raise ValueError(f"El servidor '{name}' ya existe.")
        self._sessions[name] = MCPServerSession(ServerConfig(name=name, command=command))

    def close_all(self) -> None:
        for session in self._sessions.values():
            session.close()


if __name__ == "__main__":
    client = MCPClientManager(SERVERS)

    batfish = client.server("batfish")
    batfish.start()

    try:
        print("INIT:")
        print(json.dumps(batfish.initialize(), indent=2, ensure_ascii=False))

        print("\nTOOLS:")
        print(json.dumps(batfish.list_tools(), indent=2, ensure_ascii=False))

        # Ejemplo de call:
        # print("\nCALL:")
        # print(json.dumps(
        #     batfish.call_tool("nombre_tool", {"x": 1}),
        #     indent=2,
        #     ensure_ascii=False
        # ))

        # Si luego agregas otro servidor en SERVERS:
        # otro = client.server("otro_server")
        # otro.start()
        # print(json.dumps(otro.initialize(), indent=2, ensure_ascii=False))
        # print(json.dumps(otro.list_tools(), indent=2, ensure_ascii=False))

    finally:
        client.close_all()
