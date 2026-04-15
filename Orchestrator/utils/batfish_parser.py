#!/usr/bin/env python3
"""
Normalize CLI-like model predictions into Batfish-friendly configuration files.

Input CSV requirements:
- Must contain a column named ``predictions``.
- Typically also contains ``model_name``.

Output:
- Same number of rows as input.
- Same columns as input (the script only rewrites ``predictions``).
- Each parseable prediction is converted into one or more config file blocks
  wrapped in ``[configs/routerX.cfg]`` / ``[/configs/routerX.cfg]`` markers.
- Each generated config body follows the Cisco IOS ``show running-config``
  format expected by Batfish:
    * ``!RANCID-CONTENT-TYPE: cisco`` header for guaranteed vendor detection.
    * ``hostname <device>`` for device identification.
    * Proper 1-space indentation for sub-mode commands.
    * Support for nested sub-sub-modes (e.g. ``address-family`` inside
      ``router bgp``).
    * ``!`` delimiters between hierarchical blocks.
    * ``end`` terminator.
- Predictions that are not recognizable CLI sessions, contain placeholders,
  or are too ambiguous are replaced with the literal string ``None``.

This script is intentionally conservative:
- It normalizes structure.
- It does not try to repair uncertain semantics.
- It skips operational / non-persistent commands.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

PROMPT_RE = re.compile(r"^\s*([A-Za-z0-9_./-]+)(?:\(([^)]*)\))?#\s*(.*)\s*$")

# Vendor header injected at the top of every .cfg to guarantee Batfish
# identifies the file as Cisco IOS without relying on heuristic detection.
RANCID_HEADER = "!RANCID-CONTENT-TYPE: cisco"

# ---------------------------------------------------------------------------
# Top-level block openers (depth 0 → depth 1)
# ---------------------------------------------------------------------------
BLOCK_OPENERS = [
    re.compile(r"^interface\s+\S+", re.I),
    re.compile(r"^router\s+\S+(?:\s+\S+)?", re.I),
    re.compile(r"^line\s+\S+(?:\s+\S+)?(?:\s+\S+)?", re.I),
    re.compile(r"^ip access-list\s+\S+\s+\S+", re.I),
    re.compile(r"^vlan\s+\S+", re.I),
    re.compile(r"^ip dhcp pool\s+\S+", re.I),
    # --- additions for routing-policy & security modelling ---
    re.compile(r"^route-map\s+\S+\s+\S+(?:\s+\d+)?", re.I),
    re.compile(r"^ip prefix-list\s+\S+", re.I),
    re.compile(r"^ip community-list\s+", re.I),
    re.compile(r"^key chain\s+\S+", re.I),
    re.compile(r"^crypto\s+map\s+\S+", re.I),
    re.compile(r"^crypto\s+isakmp\s+", re.I),
    re.compile(r"^ip sla\s+\d+", re.I),
    re.compile(r"^class-map\s+", re.I),
    re.compile(r"^policy-map\s+", re.I),
]

# ---------------------------------------------------------------------------
# Sub-block openers that create a second nesting level inside an already-open
# block (e.g. ``address-family`` inside ``router bgp``).
# ---------------------------------------------------------------------------
SUB_BLOCK_OPENERS = [
    re.compile(r"^address-family\s+", re.I),
    re.compile(r"^vrf\s+\S+", re.I),
]

# Commands that should never be written to the final configuration file.
SKIP_PATTERNS = [
    re.compile(r"^\s*configure terminal\b", re.I),
    re.compile(r"^\s*conf t\b", re.I),
    re.compile(r"^\s*end\b", re.I),
    re.compile(r"^\s*exit\b", re.I),
    re.compile(r"^\s*exit-address-family\s*$", re.I),
    re.compile(r"^\s*exit-vrf\s*$", re.I),
]

# Commands / patterns treated as unsafe or clearly unsuitable for this conversion.
DISALLOWED_PATTERNS = [
    re.compile(r"<[^>]+>"),  # placeholders such as <existing_outbound_ACL>
    re.compile(r"^\s*copy\s+running-config\s+startup-config\b", re.I),
    re.compile(r"^\s*write\s+memory\b", re.I),
    re.compile(r"^\s*do\s+", re.I),
    re.compile(r"^\s*crypto key generate rsa\b", re.I),  # interactive / device-stateful
    re.compile(r"^\s*no ip dhcp pool\s*$", re.I),  # incomplete: pool name missing
    re.compile(r"^\s*ip ospf authentication-mode\b", re.I),
    re.compile(r"^\s*logging buffer-size\b", re.I),
]


@dataclass
class DeviceState:
    """Tracks the lines emitted for a single device and its block nesting.

    ``depth`` represents the current hierarchical nesting:
        0 = global config
        1 = inside a block opener (interface, router, …)
        2 = inside a sub-block opener (address-family, vrf, …)
    """

    lines: List[str]
    depth: int = 0


class BatfishPredictionParser:
    def __init__(self, none_value: str = "None") -> None:
        self.none_value = none_value

    def parse_prediction(self, raw_value: object) -> str:
        text = self._normalize_text(raw_value)
        if not text:
            return self.none_value

        raw_lines = [line for line in text.split("\n") if line.strip()]
        parsed_lines = []
        non_prompt_lines = 0

        for line in raw_lines:
            match = PROMPT_RE.match(line)
            if match:
                device = match.group(1)
                mode = match.group(2) or ""
                command = match.group(3).strip()
                parsed_lines.append((device, mode, command))
            else:
                non_prompt_lines += 1

        # No CLI prompts => most likely a natural-language answer or malformed row.
        if not parsed_lines:
            return self.none_value

        # If too much content is not prompt-based, reject conservatively.
        if non_prompt_lines and (non_prompt_lines / max(len(raw_lines), 1)) > 0.20:
            return self.none_value

        devices: "OrderedDict[str, DeviceState]" = OrderedDict()
        invalid_signals = 0
        wrote_any_config = False

        def ensure_device(device_name: str) -> DeviceState:
            if device_name not in devices:
                devices[device_name] = DeviceState(lines=[])
            return devices[device_name]

        def close_to_depth(device_name: str, target: int) -> None:
            """Close open blocks until we reach *target* depth."""
            st = ensure_device(device_name)
            while st.depth > target:
                if not st.lines or st.lines[-1] != "!":
                    st.lines.append("!")
                st.depth -= 1

        for device, mode, command in parsed_lines:
            state = ensure_device(device)

            if not command:
                continue

            if self._is_skip_command(command):
                # exit / exit-address-family ─ just reduce depth by one.
                if state.depth > 0:
                    close_to_depth(device, state.depth - 1)
                continue

            if self._is_disallowed_command(command):
                invalid_signals += 1
                continue

            if self._looks_like_invalid_acl_host_network(command):
                invalid_signals += 1
                continue

            if self._looks_like_broken_inline_interface_acl(command):
                invalid_signals += 1
                continue

            if self._looks_like_truncated_ip_address(command):
                invalid_signals += 1
                continue

            # Example of invalid structure seen in the sample list:
            # router ospf 1
            #  interface Ethernet0/0       ← wrong: interface isn't a router sub-command
            if mode.lower().startswith("config-router") and re.match(
                r"^interface\s+", command, re.I
            ):
                invalid_signals += 1
                continue

            # ----- global / config mode -----
            if mode in ("", "config"):
                if self._is_block_opener(command):
                    close_to_depth(device, 0)
                    state.lines.append(command)
                    state.depth = 1
                    wrote_any_config = True
                else:
                    close_to_depth(device, 0)
                    state.lines.append(command)
                    wrote_any_config = True
            # ----- sub-mode content -----
            else:
                # Sub-block opener inside an existing block (e.g. address-family)
                if state.depth >= 1 and self._is_sub_block_opener(command):
                    # If we were already at depth 2, close back to 1 first.
                    close_to_depth(device, 1)
                    state.lines.append(f" {command}")
                    state.depth = 2
                    wrote_any_config = True
                    continue

                # Regular sub-mode content must belong to an open parent block.
                if state.depth < 1:
                    invalid_signals += 1
                    continue

                indent = " " * state.depth
                state.lines.append(f"{indent}{command}")
                wrote_any_config = True

        for device_name in list(devices.keys()):
            close_to_depth(device_name, 0)

        rendered_devices: List[str] = []
        for device_name, state in devices.items():
            body = [line for line in state.lines if line.strip()]
            if not body:
                continue
            filename = self._device_to_filename(device_name)

            # Build the full config body with Batfish-required elements:
            #   1. RANCID header  → guarantees vendor detection
            #   2. hostname       → device identification in the model
            #   3. config body    → the parsed commands
            #   4. end terminator → standard show running-config trailer
            header_lines = [
                RANCID_HEADER,
                "!",
                f"hostname {device_name}",
                "!",
            ]
            full_body = "\n".join(header_lines + body + ["!", "end"])
            rendered_devices.append(
                f"[configs/{filename}]\n{full_body}\n[/configs/{filename}]"
            )

        if not rendered_devices or not wrote_any_config:
            return self.none_value

        # Conservative rule: if every useful line was rejected, return None.
        result = "\n\n".join(rendered_devices).strip()
        if invalid_signals > 0 and not result:
            return self.none_value

        return result if result else self.none_value

    @staticmethod
    def _normalize_text(value: object) -> str:
        if value is None:
            return ""

        text = str(value).strip()
        if not text:
            return ""

        # Remove surrounding quotes if the whole cell is quoted.
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1]

        # Normalize both literal escaped newlines and actual newlines.
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text.strip()

    @staticmethod
    def _is_block_opener(command: str) -> bool:
        return any(pattern.match(command) for pattern in BLOCK_OPENERS)

    @staticmethod
    def _is_sub_block_opener(command: str) -> bool:
        """Return True if the command opens a nested sub-block (depth 2)."""
        return any(pattern.match(command) for pattern in SUB_BLOCK_OPENERS)

    @staticmethod
    def _is_skip_command(command: str) -> bool:
        return any(pattern.match(command) for pattern in SKIP_PATTERNS)

    @staticmethod
    def _is_disallowed_command(command: str) -> bool:
        return any(pattern.search(command) for pattern in DISALLOWED_PATTERNS)

    @staticmethod
    def _looks_like_invalid_acl_host_network(command: str) -> bool:
        # Example: host 10.0.14.0 0.0.0.255  -> host must not be followed by a wildcard.
        return bool(
            re.search(r"\bhost\s+\d+\.\d+\.\d+\.0\s+\d+\.\d+\.\d+\.\d+", command, re.I)
        )

    @staticmethod
    def _looks_like_broken_inline_interface_acl(command: str) -> bool:
        # Example seen in samples:
        # ip access-group PERMIT_LAN2 in interface FastEthernet0/24
        # The persistent form should be under the interface block, not inline like this.
        return bool(re.search(r"\bip access-group\b.+\bin\s+interface\s+", command, re.I))

    @staticmethod
    def _looks_like_truncated_ip_address(command: str) -> bool:
        # Example: ip address 10.0.24.2   (missing mask)
        return bool(re.match(r"^ip address\s+\d+\.\d+\.\d+\.\d+\s*$", command, re.I))

    @staticmethod
    def _device_to_filename(device_name: str) -> str:
        upper_name = device_name.upper()

        router_match = re.fullmatch(r"R(\d+)", upper_name)
        if router_match:
            return f"router{router_match.group(1)}.cfg"

        switch_match = re.fullmatch(r"SW(\d+)", upper_name)
        if switch_match:
            return f"switch{switch_match.group(1)}.cfg"

        slug = re.sub(r"[^a-z0-9_-]+", "", device_name.lower().replace(" ", "_"))
        if not slug:
            slug = "device"
        return f"{slug}.cfg"



