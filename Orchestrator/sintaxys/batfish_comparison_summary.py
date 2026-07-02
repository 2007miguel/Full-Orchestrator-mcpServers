#!/usr/bin/env python3
"""
Parse comparison JSON predictions, evaluate them with Batfish, and write one
summary CSV.

Input:  JSON files in sintaxys/comparacion enfoque/, each with a predictions list.
Output: one summary CSV with one row per JSON/model.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "few-shot"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "few-shot_summary.csv"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


PROMPT_RE = re.compile(r"^\s*([A-Za-z0-9_./-]+)(?:\(([^)]*)\))?#\s*(.*)\s*$")
RANCID_HEADER = "!RANCID-CONTENT-TYPE: cisco"

GLOBAL_MODES = {"", "config"}

DEPTH1_MODE_PREFIXES = (
    "config-if",
    "config-subif",
    "config-router",
    "config-line",
    "config-ext-nacl",
    "config-std-nacl",
    "config-nacl",
    "config-cmap",
    "config-pmap",
    "config-vlan",
    "config-dhcp",
    "config-isakmp",
    "config-isakmp-policy",
    "config-crypto-map",
    "config-sla",
    "config-key-chain",
    "config-route-map",
    "config-sec-zone",
    "config-vrf",
    "config-red",
)

DEPTH2_MODE_PREFIXES = (
    "config-router-af",
    "config-router-vrf",
    "config-if-vrrp",
    "config-if-vrrp-ipv6",
    "config-pclass",
    "config-pmap-c",
    "config-key",
    "config-sec-zone-pair",
    "config-red-app",
    "config-red-app-prtcl",
    "config-vrf-af",
    "dhcp-config",
    "config-service-group",
)


def mode_depth(mode: str) -> int:
    normalized = mode.strip().lower()
    if normalized in GLOBAL_MODES:
        return 0
    for prefix in DEPTH2_MODE_PREFIXES:
        if normalized.startswith(prefix):
            return 2
    for prefix in DEPTH1_MODE_PREFIXES:
        if normalized.startswith(prefix):
            return 1
    if normalized.startswith("config-"):
        return 1
    return 0


BLOCK_OPENERS = [
    re.compile(r"^interface\s+\S+", re.I),
    re.compile(r"^router\s+\S+(?:\s+\S+)?", re.I),
    re.compile(r"^line\s+\S+(?:\s+\S+)?(?:\s+\S+)?", re.I),
    re.compile(r"^ip\s+access-list\s+\S+\s+\S+", re.I),
    re.compile(r"^vlan\s+\d+(?:\s*,\s*\d+)*", re.I),
    re.compile(r"^ip\s+dhcp\s+pool\s+\S+", re.I),
    re.compile(r"^route-map\s+\S+(?:\s+\S+(?:\s+\d+)?)?", re.I),
    re.compile(r"^ip\s+prefix-list\s+\S+", re.I),
    re.compile(r"^ip\s+community-list\s+", re.I),
    re.compile(r"^key\s+chain\s+\S+", re.I),
    re.compile(r"^crypto\s+map\s+\S+", re.I),
    re.compile(r"^crypto\s+isakmp\s+", re.I),
    re.compile(r"^ip\s+sla\s+\d+", re.I),
    re.compile(r"^class-map\s+", re.I),
    re.compile(r"^policy-map\s+", re.I),
    re.compile(r"^zone\s+security\s+\S+", re.I),
    re.compile(r"^zone-pair\s+security\s+", re.I),
    re.compile(r"^fhrp\s+version\s+", re.I),
    re.compile(r"^redundancy\b", re.I),
    re.compile(r"^object-group\s+", re.I),
    re.compile(r"^ip\s+vrf\s+\S+", re.I),
    re.compile(r"^vrf\s+definition\s+\S+", re.I),
    re.compile(r"^router\s+ospfv3\s+\d+", re.I),
    re.compile(r"^aaa\s+", re.I),
]

SUB_BLOCK_OPENERS = [
    re.compile(r"^address-family\s+", re.I),
    re.compile(r"^vrf\s+\S+", re.I),
    re.compile(r"^class\s+\S+", re.I),
    re.compile(r"^key\s+\d+", re.I),
    re.compile(r"^application\s+redundancy", re.I),
]

SKIP_PATTERNS = [
    re.compile(r"^\s*configure\s+terminal\b", re.I),
    re.compile(r"^\s*conf\s+t\b", re.I),
    re.compile(r"^\s*end\b$", re.I),
    re.compile(r"^\s*exit\b$", re.I),
    re.compile(r"^\s*exit-address-family\s*$", re.I),
    re.compile(r"^\s*exit-vrf\s*$", re.I),
    re.compile(r"^\s*do\s+", re.I),
    re.compile(r"^\s*show\s+", re.I),
    re.compile(r"^\s*ping\s+", re.I),
    re.compile(r"^\s*traceroute\s+", re.I),
    re.compile(r"^\s*tracert\s+", re.I),
    re.compile(r"^\s*terminal\s+(?:length|width)\b", re.I),
    re.compile(r"^\s*enable\b", re.I),
    re.compile(r"^\s*disable\b", re.I),
    re.compile(r"^\s*reload\b", re.I),
    re.compile(r"^\s*clear\s+", re.I),
    re.compile(r"^\s*debug\s+", re.I),
    re.compile(r"^\s*undebug\s+", re.I),
    re.compile(r"^\s*write\s+(?:memory|erase)\b", re.I),
    re.compile(r"^\s*wr\s+(?:mem|erase)\b", re.I),
    re.compile(r"^\s*copy\s+running-config\s+startup-config\b", re.I),
    re.compile(r"^\s*copy\s+run\s+start\b", re.I),
    re.compile(r"^\s*crypto\s+key\s+generate\s+rsa\b", re.I),
]

DISALLOWED_PATTERNS = [
    re.compile(r"<[^>]+>"),
]

FULL_BLOCK_RE = re.compile(
    r"\[configs/(?P<name>[^\]]+\.cfg)\]\s*\n?(?P<body>.*?)\n?\[/configs/(?P=name)\]",
    re.IGNORECASE | re.DOTALL,
)

STATUS_SCORE_MAP = {
    "PASSED": 1.0,
    "FAILED": 0.0,
    "UNKNOWN": 0.0,
}


@dataclass
class DeviceState:
    lines: List[str] = field(default_factory=list)
    depth: int = 0
    has_hostname: bool = False


@dataclass
class VerificationResult:
    pqs_modified: float
    total_modified_files: int
    parsed_files: int
    parse_status_counts: Dict[str, int]


class BatfishPredictionParser:
    def __init__(self, none_value: str = "None") -> None:
        self.none_value = none_value

    def parse_prediction(self, raw_value: object) -> str:
        text = self._normalize_text(raw_value)
        if not text:
            return self.none_value

        parsed_lines: List[Tuple[str, str, str]] = []
        for line in [line for line in text.split("\n") if line.strip()]:
            match = PROMPT_RE.match(line)
            if match:
                parsed_lines.append(
                    (match.group(1), match.group(2) or "", match.group(3).strip())
                )

        if not parsed_lines:
            return self.none_value

        devices: OrderedDict[str, DeviceState] = OrderedDict()

        def ensure(name: str) -> DeviceState:
            if name not in devices:
                devices[name] = DeviceState()
            return devices[name]

        def close_to(state: DeviceState, target: int) -> None:
            while state.depth > target:
                if not state.lines or state.lines[-1] != "!":
                    state.lines.append("!")
                state.depth -= 1

        for device, mode, command in parsed_lines:
            if not command:
                continue
            if self._is_skip(command):
                close_to(ensure(device), max(0, mode_depth(mode) - 1))
                continue
            if self._is_disallowed(command):
                return self.none_value

            state = ensure(device)
            target_depth = mode_depth(mode)

            if target_depth == 0:
                close_to(state, 0)
                if command.lower().startswith("hostname "):
                    state.has_hostname = True
                state.lines.append(command)
                if self._is_block_opener(command):
                    state.depth = 1
            elif target_depth == 1:
                if self._is_block_opener(command):
                    close_to(state, 0)
                    state.lines.append(command)
                    state.depth = 1
                elif self._is_sub_block_opener(command):
                    if state.depth < 1:
                        return self.none_value
                    close_to(state, 1)
                    state.lines.append(f" {command}")
                    state.depth = 2
                else:
                    if state.depth < 1:
                        return self.none_value
                    close_to(state, 1)
                    state.lines.append(f" {command}")
            else:
                if self._is_sub_block_opener(command):
                    if state.depth < 1:
                        return self.none_value
                    close_to(state, 1)
                    state.lines.append(f" {command}")
                    state.depth = 2
                else:
                    if state.depth < 2:
                        return self.none_value
                    state.lines.append(f"  {command}")

        for state in devices.values():
            close_to(state, 0)

        rendered: List[str] = []
        for device_name, state in devices.items():
            body = [line for line in state.lines if line.strip()]
            if not body:
                continue

            filename = self._device_to_filename(device_name)
            header = [RANCID_HEADER, "!"]
            if not state.has_hostname:
                header += [f"hostname {device_name}", "!"]

            full_body = "\n".join(header + body + ["!", "end"])
            rendered.append(
                f"[configs/{filename}]\n{full_body}\n[/configs/{filename}]"
            )

        return "\n\n".join(rendered).strip() if rendered else self.none_value

    @staticmethod
    def _normalize_text(value: object) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1]
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text.strip()

    @staticmethod
    def _is_skip(command: str) -> bool:
        return any(pattern.match(command) for pattern in SKIP_PATTERNS)

    @staticmethod
    def _is_disallowed(command: str) -> bool:
        return any(pattern.search(command) for pattern in DISALLOWED_PATTERNS)

    @staticmethod
    def _is_block_opener(command: str) -> bool:
        return any(pattern.match(command) for pattern in BLOCK_OPENERS)

    @staticmethod
    def _is_sub_block_opener(command: str) -> bool:
        return any(pattern.match(command) for pattern in SUB_BLOCK_OPENERS)

    @staticmethod
    def _device_to_filename(device_name: str) -> str:
        upper = device_name.upper()
        router_match = re.fullmatch(r"R(\d+)", upper)
        if router_match:
            return f"router{router_match.group(1)}.cfg"
        switch_match = re.fullmatch(r"SW(\d+)", upper)
        if switch_match:
            return f"switch{switch_match.group(1)}.cfg"
        slug = re.sub(r"[^a-z0-9_-]+", "", device_name.lower().replace(" ", "_"))
        return f"{slug or 'device'}.cfg"


def create_project_mcp_client() -> Any:
    import mcp_client

    manager_class = getattr(mcp_client, "MCPClientManager", None)
    servers_config = getattr(mcp_client, "SERVERS", None)
    if not (manager_class and callable(manager_class)):
        raise AttributeError("Could not find MCPClientManager in mcp_client.py")
    if not isinstance(servers_config, dict):
        raise AttributeError("Could not find SERVERS in mcp_client.py")
    return manager_class(servers_config)


def count_modified_files(text: str) -> int:
    return sum(1 for _ in FULL_BLOCK_RE.finditer(text))


def normalize_status(status: Any) -> str:
    if status is None:
        return "UNKNOWN"
    normalized = str(status).strip().upper().replace("-", "_").replace(" ", "_")
    if "PARTIAL" in normalized:
        return "PARTIALLY_PARSED"
    return normalized or "UNKNOWN"


def status_to_score(status: Any) -> float:
    normalized = normalize_status(status)
    if normalized == "PARTIALLY_PARSED":
        return 0.5
    return STATUS_SCORE_MAP.get(normalized, 0.0)


def unwrap_mcp_response(response: Any) -> Any:
    if response is None:
        return None
    if isinstance(response, (list, tuple)):
        return response
    if isinstance(response, str):
        try:
            return json.loads(response.strip())
        except Exception:
            return response
    if isinstance(response, dict):
        if "structuredContent" in response:
            return response["structuredContent"]
        if "content" in response and isinstance(response["content"], list):
            for item in response["content"]:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        try:
                            return json.loads(text)
                        except Exception:
                            pass
        return response
    return response


def extract_file_parse_results(status_response: Any) -> List[Dict[str, Any]]:
    payload = unwrap_mcp_response(status_response)
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            return [item for item in payload["results"] if isinstance(item, dict)]
        for key in ("data", "result", "payload"):
            inner = payload.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("results"), list):
                return [item for item in inner["results"] if isinstance(item, dict)]
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


class BatfishPQSEvaluator:
    def __init__(self, mcp_client: Any):
        self.mcp_client = mcp_client
        from utils.create_snapshot import create_snapshot_from_string

        self.create_snapshot_from_string = create_snapshot_from_string

    def verify_prediction(self, prediction_text: str) -> VerificationResult:
        if not prediction_text or prediction_text == "None":
            return VerificationResult(0.0, 0, 0, {})

        total_modified_files = count_modified_files(prediction_text)
        if total_modified_files == 0:
            return VerificationResult(0.0, 0, 0, {})

        temp_root = tempfile.mkdtemp(prefix="snapshot_verify_")
        try:
            zip_path = self.create_snapshot_from_string(prediction_text, base_dir=temp_root)
            if not zip_path:
                return VerificationResult(0.0, total_modified_files, 0, {})

            batfish_server = self.mcp_client.server("batfish")
            batfish_server.call_tool("load_snapshot", {"zip_path": zip_path})
            status_response = batfish_server.call_tool("file_parse_status", {})
            results = extract_file_parse_results(status_response)

            scores: List[float] = []
            status_counts: Dict[str, int] = {}
            for item in results:
                normalized = normalize_status(item.get("status"))
                status_counts[normalized] = status_counts.get(normalized, 0) + 1
                scores.append(status_to_score(normalized))

            pqs_modified = sum(scores) / total_modified_files if total_modified_files else 0.0
            return VerificationResult(
                float(pqs_modified),
                total_modified_files,
                len(results),
                status_counts,
            )
        except Exception:
            return VerificationResult(0.0, total_modified_files, 0, {})
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


def summarize_model(
    model_name: str,
    predictions: List[Any],
    parser: BatfishPredictionParser,
    evaluator: BatfishPQSEvaluator,
) -> Dict[str, Any]:
    print(f"[{model_name}] Evaluating {len(predictions)} predictions...")

    pqs_values: List[float] = []
    total_modified_files = 0
    total_passed = 0
    total_partially_parsed = 0
    total_failed = 0
    total_unknown = 0

    for index, prediction in enumerate(predictions, start=1):
        if index == 1 or index == len(predictions) or index % 10 == 0:
            print(f"[{model_name}] {index}/{len(predictions)}")

        parsed_config = parser.parse_prediction(prediction)
        result = evaluator.verify_prediction(parsed_config)

        pqs_values.append(result.pqs_modified)
        total_modified_files += result.total_modified_files
        total_passed += result.parse_status_counts.get("PASSED", 0)
        total_partially_parsed += result.parse_status_counts.get("PARTIALLY_PARSED", 0)
        total_failed += result.parse_status_counts.get("FAILED", 0)
        total_unknown += result.parse_status_counts.get("UNKNOWN", 0)

    print(f"[{model_name}] Done")

    return {
        "model_name": model_name,
        "rows": len(predictions),
        "avg_pqs_modified": mean(pqs_values) if pqs_values else 0.0,
        "median_pqs_modified": median(pqs_values) if pqs_values else 0.0,
        "total_modified_files": total_modified_files,
        "total_passed": total_passed,
        "total_partially_parsed": total_partially_parsed,
        "total_failed": total_failed,
        "total_unknown": total_unknown,
    }


def load_predictions(json_path: Path) -> List[Any]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    predictions = data.get("predictions")
    if not isinstance(predictions, list):
        raise ValueError(f"{json_path} does not contain a predictions list")
    return predictions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate Batfish syntax summary for comparison JSON files."
    )
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_CSV))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir)
    output_csv = Path(args.output)

    print(f"Reading JSON files from: {input_dir}")
    json_files = sorted(input_dir.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"No JSON files found in: {input_dir}")
    print(f"Found {len(json_files)} JSON files")

    print("Starting Batfish MCP server...")
    mcp_client = create_project_mcp_client()
    batfish_server = mcp_client.server("batfish")
    batfish_server.start()
    batfish_server.initialize()
    print("Batfish MCP server ready")

    parser = BatfishPredictionParser()
    evaluator = BatfishPQSEvaluator(mcp_client)

    try:
        rows = [
            summarize_model(
                json_path.stem,
                load_predictions(json_path),
                parser,
                evaluator,
            )
            for json_path in json_files
        ]
    finally:
        print("Closing MCP connections...")
        mcp_client.close_all()

    fieldnames = [
        "model_name",
        "rows",
        "avg_pqs_modified",
        "median_pqs_modified",
        "total_modified_files",
        "total_passed",
        "total_partially_parsed",
        "total_failed",
        "total_unknown",
    ]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Summary written to: {output_csv}")


if __name__ == "__main__":
    main()
