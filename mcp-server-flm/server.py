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

@mcp.tool()
def retrieve_chunks(
    semantic_query: str,
    device_type: str = "unknown",
    os: str = "IOS XE",
    version: str = "17",
    product: str = "unknown",
    k: int = 5,
    candidate_k: int = 12,
) -> CallToolResult:
    if not semantic_query or not isinstance(semantic_query, str):
        return CallToolResult(
            content=[TextContent(type="text", text="semantic_query inválido")],
            isError=True,
        )

    endpoint = _get_flm_endpoint()

    payload = {
        "semantic_query": semantic_query,
        "device_type": device_type,
        "os": os,
        "version": version,
        "product": product,
        "k": k,
        "candidate_k": candidate_k,
    }

    try:
        resp = requests.post(endpoint, json=payload, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error de comunicación: {exc}")],
            isError=True,
        )

    try:
        data = resp.json()
    except ValueError:
        return CallToolResult(
            content=[TextContent(type="text", text=resp.text)],
            isError=False,
        )

    chunks = data.get("chunks", [])
    if not chunks:
        return CallToolResult(
            content=[TextContent(type="text", text="Sin resultados para la query.")],
            isError=False,
        )

    lines = [f"Chunks recuperados: {data.get('count', len(chunks))}\n"]
    for i, c in enumerate(chunks, 1):
        lines.append(
            f"[{i}] score={c.get('score', 0):.4f} | "
            f"{c.get('device_type', '')} | {c.get('product', '')} | "
            f"{c.get('section', '')}\n"
            f"    Commands: {str(c.get('commands', ''))[:120]}"
        )

    return CallToolResult(
        content=[TextContent(type="text", text="\n".join(lines))],
        isError=False,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")