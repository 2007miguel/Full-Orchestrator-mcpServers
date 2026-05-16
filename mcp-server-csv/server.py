from mcp.server.fastmcp import FastMCP
from pathlib import Path
import csv
import os
from typing import Any

mcp = FastMCP("csv-store")

def get_default_documents_dir() -> Path:
    """
    Detecta la carpeta Documents de forma portable.
    """
    home = Path.home()

    # Windows típico
    docs_win = home / "Documents"
    # Linux/WSL/Mac
    docs_unix = home / "Documents"

    # Si existe, usarla; si no, fallback al home
    if docs_win.exists():
        return docs_win / "csv_data"
    elif docs_unix.exists():
        return docs_unix / "csv_data"
    else:
        return home / "csv_data" 
    
BASE_DIR = Path(
    os.getenv("CSV_STORAGE_DIR", get_default_documents_dir())
).resolve()

BASE_DIR.mkdir(parents=True, exist_ok=True)


def _safe_csv_path(filename: str) -> Path:
    if not filename.endswith(".csv"):
        filename += ".csv"

    path = (BASE_DIR / filename).resolve()

    if BASE_DIR != path.parent and BASE_DIR not in path.parents:
        raise ValueError("Ruta no permitida fuera de CSV_STORAGE_DIR")

    return path


@mcp.tool()
def create_csv(filename: str, headers: list[str]) -> dict[str, Any]:
    """
    Create a CSV file with the given headers.
    """
    path = _safe_csv_path(filename)

    if path.exists():
        return {
            "ok": False,
            "message": f"El archivo ya existe: {path.name}",
        }

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()

    return {
        "ok": True,
        "file": str(path),
        "headers": headers,
    }


@mcp.tool()
def append_row(filename: str, row: dict[str, Any]) -> dict[str, Any]:
    """
    Append one row to an existing CSV file.
    """
    path = _safe_csv_path(filename)

    if not path.exists():
        return {
            "ok": False,
            "message": f"No existe el archivo: {path.name}",
        }

    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

    missing = [h for h in headers if h not in row]
    extra = [k for k in row.keys() if k not in headers]

    if extra:
        return {
            "ok": False,
            "message": "La fila contiene columnas no definidas en el CSV",
            "extra_columns": extra,
            "expected_headers": headers,
        }

    clean_row = {h: row.get(h, "") for h in headers}

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writerow(clean_row)

    return {
        "ok": True,
        "file": str(path),
        "row": clean_row,
        "missing_columns_filled_empty": missing,
    }


@mcp.tool()
def read_csv(filename: str, limit: int = 20) -> dict[str, Any]:
    """
    Read rows from a CSV file.
    """
    path = _safe_csv_path(filename)

    if not path.exists():
        return {
            "ok": False,
            "message": f"No existe el archivo: {path.name}",
        }

    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    return {
        "ok": True,
        "file": str(path),
        "total_rows": len(rows),
        "rows": rows[:limit],
    }


@mcp.tool()
def list_csv_files() -> dict[str, Any]:
    """
    List available CSV files.
    """
    files = sorted([p.name for p in BASE_DIR.glob("*.csv")])

    return {
        "ok": True,
        "base_dir": str(BASE_DIR),
        "files": files,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")