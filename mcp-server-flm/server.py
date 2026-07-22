from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urljoin

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

mcp = FastMCP("llm_prompt_mcp")

FLM_BASE_URL_ENV = "FLM_BASE_URL"
FLM_ENDPOINT_ENV = "FLM_ENDPOINT_URL"
DEFAULT_FLM_BASE_URL = "https://accustom-smooth-trilogy.ngrok-free.dev"


def _get_flm_base_url() -> str:
    endpoint = os.environ.get(FLM_BASE_URL_ENV) or os.environ.get(FLM_ENDPOINT_ENV)
    endpoint = endpoint or DEFAULT_FLM_BASE_URL

    if not isinstance(endpoint, str) or not endpoint.strip():
        raise RuntimeError("El endpoint del FLM no esta configurado correctamente")

    endpoint = endpoint.strip().rstrip("/")
    for suffix in ["/generate", "/retrieve"]:
        if endpoint.endswith(suffix):
            endpoint = endpoint[: -len(suffix)]
            break

    return endpoint


def _get_flm_endpoint(route: str) -> str:
    route = route.strip("/")
    return urljoin(_get_flm_base_url() + "/", route)


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
def send_prompt(prompt: str, max_new_tokens: int = 0) -> CallToolResult:
    if not prompt or not isinstance(prompt, str):
        return CallToolResult(
            content=[TextContent(type="text", text="Prompt invalido")],
            isError=True,
        )

    endpoint = _get_flm_endpoint("generate")

    payload = {"prompt": prompt}
    # 0 = no override: the Colab server applies its default (MAX_NEW_TOKENS).
    # Intent normalization passes 120 (v17 MAX_NEW_TOKENS_INTENT).
    if isinstance(max_new_tokens, int) and max_new_tokens > 0:
        payload["max_new_tokens"] = max_new_tokens

    try:
        resp = requests.post(endpoint, json=payload, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error de comunicacion: {exc}")],
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
    device_type: list[str],
    os: list[str],
    version: list[str],
    product: list[str],
    requirement: str = "",
    k: int = 4,
) -> CallToolResult:
    if not semantic_query or not isinstance(semantic_query, str):
        return CallToolResult(
            content=[TextContent(type="text", text="semantic_query invalido")],
            isError=True,
        )

    endpoint = _get_flm_endpoint("retrieve")

    payload = {
        "semantic_query": semantic_query,
        # Raw requirement for router/switch scope inference (v17). Empty string
        # makes the server fall back to semantic_query, preserving old behavior.
        "requirement": requirement,
        "arch_base": {
            "device_type": device_type,
            "os": os,
            "version": version,
            "product": product,
        },
        "k": k,
    }

    try:
        resp = requests.post(endpoint, json=payload, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error de comunicacion: {exc}")],
            isError=True,
        )

    try:
        data = resp.json()
    except ValueError:
        return CallToolResult(
            content=[TextContent(type="text", text=resp.text)],
            isError=True,
        )

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        structuredContent=data,
        isError=False,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
