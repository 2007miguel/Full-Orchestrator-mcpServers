from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from batfish_client import BatfishClient


mcp = FastMCP("batfish_snapshot_input_mcp", json_response=True)
batfish = BatfishClient()

ACTIVE_SNAPSHOT_ENV = "BATFISH_ACTIVE_SNAPSHOT"


def _normalize_snapshot_root(extract_dir: Path) -> Path:
    items = [p for p in extract_dir.iterdir()]
    if len(items) == 1 and items[0].is_dir():
        return items[0]
    return extract_dir


def _extract_snapshot(zip_path: str) -> str:
    zip_file = Path(zip_path)

    if not zip_file.exists():
        raise FileNotFoundError(f"No existe el archivo ZIP: {zip_path}")

    if not zip_file.is_file():
        raise ValueError(f"La ruta no apunta a un archivo válido: {zip_path}")

    if zip_file.suffix.lower() != ".zip":
        raise ValueError(f"El archivo debe ser .zip: {zip_path}")

    if not zipfile.is_zipfile(zip_file):
        raise ValueError(f"El archivo no es un ZIP válido: {zip_path}")

    extract_dir = Path(tempfile.mkdtemp(prefix="batfish_snapshot_"))

    with zipfile.ZipFile(zip_file, "r") as zf:
        zf.extractall(extract_dir)

    snapshot_root = _normalize_snapshot_root(extract_dir)

    configs_dir = snapshot_root / "configs"
    if not configs_dir.exists() or not configs_dir.is_dir():
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise ValueError(
            "El snapshot descomprimido no contiene la carpeta obligatoria 'configs/'"
        )

    os.environ[ACTIVE_SNAPSHOT_ENV] = str(snapshot_root)
    return str(snapshot_root)


def _get_active_snapshot() -> str:
    snapshot_path = os.environ.get(ACTIVE_SNAPSHOT_ENV)

    if not snapshot_path:
        raise RuntimeError("No hay snapshot activo cargado")

    if not Path(snapshot_path).exists():
        raise RuntimeError(
            f"La ruta del snapshot activo ya no existe: {snapshot_path}"
        )

    return snapshot_path


@mcp.tool()
def load_snapshot(zip_path: str) -> CallToolResult:
    if not zip_path or not isinstance(zip_path, str):
        raise ValueError("El parámetro 'zip_path' es obligatorio y debe ser string")

    snapshot_path = _extract_snapshot(zip_path)
    result = batfish.load_snapshot(snapshot_path)

    return CallToolResult(
        content=[],
        structuredContent=result,
        isError=False,
    )


@mcp.tool()
def file_parse_status() -> CallToolResult:
    _get_active_snapshot()
    result = batfish.file_parse_status()

    return CallToolResult(
        content=[],
        structuredContent=result,
        isError=False,
    )


@mcp.tool()
def parse_warning(aggregate_duplicates: bool = True) -> CallToolResult:
    _get_active_snapshot()

    if not isinstance(aggregate_duplicates, bool):
        raise ValueError("El parámetro 'aggregate_duplicates' debe ser booleano")

    result = batfish.parse_warning(aggregate_duplicates=aggregate_duplicates)

    return CallToolResult(
        content=[],
        structuredContent=result,
        isError=False,
    )


@mcp.tool()
def init_issues() -> CallToolResult:
    _get_active_snapshot()
    result = batfish.init_issues()

    return CallToolResult(
        content=[],
        structuredContent=result,
        isError=False,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")