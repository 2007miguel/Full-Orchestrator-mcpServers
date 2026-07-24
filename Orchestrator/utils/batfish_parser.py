#!/usr/bin/env python3
"""
Normalize CLI-like model predictions into Batfish-friendly configuration files.

SINGLE SOURCE OF TRUTH for ``BatfishPredictionParser`` — used both by the
orchestrator pipeline (``core/execution_controller.py``) and by the offline
evaluation harness (``sintaxys/batfish_comparison_summary.py``), which now
imports it from here instead of keeping a divergent copy.

Consolidated from the two historical copies, keeping the best of each:
- Depth is derived from the prompt mode label (explicit depth-1 / depth-2
  sub-modes), which robustly handles nested blocks like ``address-family``.
- A top-level block opener (interface, router, line, ...) ALWAYS opens a block
  regardless of the labeled mode, because models frequently mislabel or omit
  the opener's mode (e.g. tag ``interface Ethernet0/1`` as ``config-if``).
- Broad skip list for operational / non-persistent commands (show, ping, do,
  clear, debug, write, copy, bare ``enable``/``disable``, ...). NOTE: the bare
  ``enable`` skip is scoped so it does NOT eat ``enable secret``/``enable
  password`` (those are real configuration commands).
- Placeholder tokens (``<password>``) reject the WHOLE config, so verification
  never silently drops the line and reports a misleading "passed".
- Hostname is injected only if the config does not already define one.

Output: each parseable device becomes a ``[configs/<name>.cfg] ... [/...]``
block in Cisco IOS ``show running-config`` style (RANCID header, hostname,
1-space nesting, ``!`` delimiters, ``end`` terminator). Unparseable or empty
input returns ``none_value``.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Tuple

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
    "dhcp-config",
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
    "config-service-group",
)


def mode_depth(mode: str) -> int:
    """Infer the nesting depth (0/1/2) from the CLI prompt mode label."""
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


# Top-level block openers (depth 0 -> depth 1).
BLOCK_OPENERS = [
    re.compile(r"^interface\s+\S+", re.I),
    re.compile(r"^router\s+\S+(?:\s+\S+)?", re.I),
    re.compile(r"^line\s+\S+(?:\s+\S+)?(?:\s+\S+)?", re.I),
    re.compile(r"^ip\s+access-list\s+\S+\s+\S+", re.I),
    re.compile(r"^mac\s+access-list\s+extended\s+\S+", re.I),
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
    re.compile(r"^aaa\s+group\s+", re.I),  # only 'aaa group server ...' opens a block
]

# Sub-block openers that create a second nesting level inside an open block.
SUB_BLOCK_OPENERS = [
    re.compile(r"^address-family\s+", re.I),
    re.compile(r"^vrf\s+\S+", re.I),
    re.compile(r"^class\s+\S+", re.I),
    re.compile(r"^key\s+\d+", re.I),
    re.compile(r"^application\s+redundancy", re.I),
]

# Operational / non-persistent commands: dropped, but the rest is kept.
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
    # Bare 'enable' / 'enable <level>' only. Does NOT match 'enable secret' or
    # 'enable password', which are persistent configuration commands.
    re.compile(r"^\s*enable(?:\s+\d+)?\s*$", re.I),
    re.compile(r"^\s*disable\s*$", re.I),
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

# Placeholders like <password> => reject the whole config so verification never
# silently drops the line and reports a misleading "passed".
DISALLOWED_PATTERNS = [
    re.compile(r"<[^>]+>"),
]


@dataclass
class DeviceState:
    """Tracks the emitted lines and block nesting for a single device."""

    lines: List[str] = field(default_factory=list)
    depth: int = 0
    has_hostname: bool = False


class BatfishPredictionParser:
    def __init__(self, none_value: str = "None") -> None:
        self.none_value = none_value

    def parse_prediction(self, raw_value: object) -> str:
        text = self._normalize_text(raw_value)
        if not text:
            return self.none_value

        parsed_lines: List[Tuple[str, str, str]] = []
        for line in [ln for ln in text.split("\n") if ln.strip()]:
            match = PROMPT_RE.match(line)
            if match:
                parsed_lines.append(
                    (match.group(1), match.group(2) or "", match.group(3).strip())
                )

        # No CLI prompts => most likely natural-language text or malformed row.
        if not parsed_lines:
            return self.none_value

        devices: "OrderedDict[str, DeviceState]" = OrderedDict()

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

            # A top-level block opener always starts a new block, regardless of
            # the prompt mode the model labeled. Models frequently mislabel or
            # omit the opener's mode (e.g. tag 'interface Ethernet0/1' as
            # config-if); without this, valid configs are wrongly rejected.
            if self._is_block_opener(command):
                close_to(state, 0)
                state.lines.append(command)
                state.depth = 1
                continue

            target_depth = mode_depth(mode)

            if target_depth == 0:
                close_to(state, 0)
                if command.lower().startswith("hostname "):
                    state.has_hostname = True
                state.lines.append(command)
            elif target_depth == 1:
                if self._is_sub_block_opener(command):
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
            else:  # target_depth == 2
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
            rendered.append(f"[configs/{filename}]\n{full_body}\n[/configs/{filename}]")

        return "\n\n".join(rendered).strip() if rendered else self.none_value

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
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
