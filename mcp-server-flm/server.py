from __future__ import annotations

import os
import json
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

mcp = FastMCP("llm_prompt_mcp")

FLM_ENDPOINT_ENV = "FLM_ENDPOINT_URL"
DEFAULT_FLM_ENDPOINT = "http://localhost:8185/generate"


def _get_flm_endpoint() -> str:
    endpoint = os.environ.get(FLM_ENDPOINT_ENV, DEFAULT_FLM_ENDPOINT)
    if not isinstance(endpoint, str) or not endpoint:
        raise RuntimeError("El endpoint del FLM no está configurado correctamente")
    return endpoint


def _extract_simple_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ["output", "text", "respuesta"]:
            if key in data:
                return _extract_simple_text(data[key])
        for value in data.values():
            return _extract_simple_text(value)
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
def send_prompt(prompt: str) -> CallToolResult:
    if not prompt or not isinstance(prompt, str):
        return CallToolResult(
            content=[TextContent(type="text", text="Prompt inválido")],
            isError=True,
        )

    endpoint = _get_flm_endpoint()

    try:
        resp = requests.post(endpoint, json={"prompt": prompt}, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error de comunicación: {exc}")],
            isError=True,
        )

    try:
        payload = resp.json()
    except ValueError:
        payload = resp.text

    output_text = _extract_simple_text(payload)

    return CallToolResult(
        content=[TextContent(type="text", text=output_text)],
        isError=False,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")