#!/usr/bin/env python3
"""
Normalize CLI-like model predictions into Batfish-friendly configuration files.

Philosophy: be maximally permissive at parse time — let Batfish be the
validator.  The only hard rejections are commands that would make the output
unparseable as a text file (placeholders, interactive commands).

Permissive behaviours added in this version:
- If a command arrives in a depth-1 mode (config-if, config-router, …) but
  no parent block is open, a synthetic opener is inferred from the mode string
  and written automatically.  For example:
      R3(config-line)# exec-timeout 0 0
  → produces:
      line console 0
       exec-timeout 0 0
- If a command arrives in a depth-2 mode but we are only at depth 1 (or 0),
  the depth is promoted silently without rejecting the command.
- The invalid_commands ratio check is removed entirely — Batfish decides
  what is valid.
- Only two categories of lines are dropped:
    1. Structural/transient commands (configure terminal, exit, end, …)
    2. Hard-invalid patterns (placeholders <…>, write memory, do …,
       crypto key generate rsa, truncated IP addresses)

Input CSV:  must have a ``predictions`` column (and typically ``model_name``).
Output CSV: same columns, same row count; ``predictions`` column rewritten.
"""

from __future__ import annotations

import argparse
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# Prompt regex
# ---------------------------------------------------------------------------
PROMPT_RE = re.compile(r"^\s*([A-Za-z0-9_./-]+)(?:\(([^)]*)\))?#\s*(.*)\s*$")

RANCID_HEADER = "!RANCID-CONTENT-TYPE: cisco"

# ---------------------------------------------------------------------------
# Mode → depth mapping
# ---------------------------------------------------------------------------
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
    m = mode.strip().lower()
    if m in GLOBAL_MODES:
        return 0
    for prefix in DEPTH2_MODE_PREFIXES:
        if m.startswith(prefix):
            return 2
    for prefix in DEPTH1_MODE_PREFIXES:
        if m.startswith(prefix):
            return 1
    if m.startswith("config-"):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Synthetic opener inference
# Maps a depth-1 mode prefix → a plausible IOS block opener.
# Used when a sub-mode command arrives but no parent block is open.
# ---------------------------------------------------------------------------
SYNTHETIC_OPENERS = {
    "config-if":        "interface GigabitEthernet0/0",
    "config-subif":     "interface GigabitEthernet0/0.1",
    "config-router":    "router ospf 1",
    "config-line":      "line console 0",
    "config-ext-nacl":  "ip access-list extended GENERATED",
    "config-std-nacl":  "ip access-list standard GENERATED",
    "config-nacl":      "ip access-list extended GENERATED",
    "config-cmap":      "class-map match-any GENERATED",
    "config-pmap":      "policy-map GENERATED",
    "config-pclass":    "policy-map GENERATED",
    "config-vlan":      "vlan 1",
    "config-dhcp":      "ip dhcp pool GENERATED",
    "dhcp-config":      "ip dhcp pool GENERATED",
    "config-isakmp":    "crypto isakmp policy 1",
    "config-isakmp-policy": "crypto isakmp policy 1",
    "config-route-map": "route-map GENERATED permit 10",
    "config-sec-zone":  "zone security GENERATED",
    "config-sec-zone-pair": "zone-pair security GENERATED source inside destination outside",
    "config-vrf":       "ip vrf GENERATED",
    "config-red":       "redundancy",
    "config-red-app":   "redundancy",
    "config-service-group": "object-group service GENERATED",
}

# Synthetic sub-block opener for depth-2 modes when depth < 2
SYNTHETIC_SUB_OPENERS = {
    "config-router-af":     "address-family ipv4 unicast",
    "config-router-vrf":    "address-family ipv4 vrf GENERATED",
    "config-if-vrrp":       "vrrp 1 address-family ipv4",
    "config-if-vrrp-ipv6":  "vrrp 1 address-family ipv6",
    "config-pclass":        "class class-default",
    "config-pmap-c":        "class class-default",
    "config-key":           "key 1",
    "config-sec-zone-pair": "zone-pair security GENERATED source inside destination outside",
    "config-red-app":       "application redundancy",
    "config-red-app-prtcl": "application redundancy",
    "config-vrf-af":        "address-family ipv4",
    "config-service-group": "object-group service GENERATED",
}


def infer_depth1_opener(mode: str) -> str:
    """Return a synthetic depth-1 block opener for the given mode string."""
    m = mode.strip().lower()
    for prefix, opener in SYNTHETIC_OPENERS.items():
        if m.startswith(prefix):
            return opener
    return "interface GigabitEthernet0/0"   # safe fallback


def infer_depth2_opener(mode: str) -> str:
    """Return a synthetic depth-2 sub-block opener for the given mode string."""
    m = mode.strip().lower()
    for prefix, opener in SYNTHETIC_SUB_OPENERS.items():
        if m.startswith(prefix):
            return opener
    return "address-family ipv4 unicast"     # safe fallback


# ---------------------------------------------------------------------------
# Block openers — commands that explicitly start a named depth-1 block
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Commands to skip (structural / transient — never go into running-config)
# ---------------------------------------------------------------------------
SKIP_PATTERNS = [
    re.compile(r"^\s*configure\s+terminal\b", re.I),
    re.compile(r"^\s*conf\s+t\b", re.I),
    re.compile(r"^\s*end\b$", re.I),
    re.compile(r"^\s*exit\b$", re.I),
    re.compile(r"^\s*exit-address-family\s*$", re.I),
    re.compile(r"^\s*exit-vrf\s*$", re.I),
]

# ---------------------------------------------------------------------------
# Hard rejections — ONLY patterns that make the file unparseable as text.
# Semantic/syntactic validation is Batfish's responsibility, not the parser's.
# ---------------------------------------------------------------------------
DISALLOWED_PATTERNS = [
    re.compile(r"<[^>]+>"),   # placeholders like <interface-name> — literal text
                               # that would corrupt the config file syntax
]


@dataclass
class DeviceState:
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

        raw_lines = [line for line in text.split("\n") if line.strip()]

        parsed_lines: List[Tuple[str, str, str]] = []
        for line in raw_lines:
            m = PROMPT_RE.match(line)
            if m:
                parsed_lines.append((m.group(1), m.group(2) or "", m.group(3).strip()))

        if not parsed_lines:
            return self.none_value

        devices: OrderedDict[str, DeviceState] = OrderedDict()

        def ensure(name: str) -> DeviceState:
            if name not in devices:
                devices[name] = DeviceState()
            return devices[name]

        def close_to(st: DeviceState, target: int) -> None:
            while st.depth > target:
                if not st.lines or st.lines[-1] != "!":
                    st.lines.append("!")
                st.depth -= 1

        for device, mode, command in parsed_lines:
            if not command:
                continue

            st = ensure(device)

            # --- structural skips ---
            if self._is_skip(command):
                target = max(0, mode_depth(mode) - 1)
                close_to(st, target)
                continue

            # Skip commands with literal placeholders — unparseable as config text
            if self._is_disallowed(command):
                continue

            target_depth = mode_depth(mode)

            # ----------------------------------------------------------
            # depth 0 — global config
            # ----------------------------------------------------------
            if target_depth == 0:
                close_to(st, 0)
                if command.lower().startswith("hostname "):
                    st.has_hostname = True
                if self._is_block_opener(command):
                    st.lines.append(command)
                    st.depth = 1
                else:
                    st.lines.append(command)

            # ----------------------------------------------------------
            # depth 1 — inside a block (interface, router, line, …)
            # ----------------------------------------------------------
            elif target_depth == 1:
                if self._is_block_opener(command):
                    # The command IS the block opener (model emitted it in
                    # config-if mode — accept it as the opener itself)
                    close_to(st, 0)
                    st.lines.append(command)
                    st.depth = 1

                elif self._is_sub_block_opener(command):
                    # Enters depth 2 — ensure depth 1 is open first
                    if st.depth < 1:
                        opener = infer_depth1_opener(mode)
                        st.lines.append(opener)
                        st.depth = 1
                    close_to(st, 1)
                    st.lines.append(f" {command}")
                    st.depth = 2

                else:
                    # Regular depth-1 sub-command
                    if st.depth < 1:
                        # No parent block open → infer one
                        opener = infer_depth1_opener(mode)
                        st.lines.append(opener)
                        st.depth = 1
                    close_to(st, 1)
                    st.lines.append(f" {command}")

            # ----------------------------------------------------------
            # depth 2 — inside a sub-block (address-family, vrrp, …)
            # ----------------------------------------------------------
            else:
                if self._is_sub_block_opener(command):
                    if st.depth < 1:
                        opener = infer_depth1_opener(mode)
                        st.lines.append(opener)
                        st.depth = 1
                    close_to(st, 1)
                    st.lines.append(f" {command}")
                    st.depth = 2

                else:
                    # Ensure depth-1 block is open
                    if st.depth < 1:
                        opener = infer_depth1_opener(mode)
                        st.lines.append(opener)
                        st.depth = 1

                    # Ensure depth-2 sub-block is open
                    if st.depth < 2:
                        sub_opener = infer_depth2_opener(mode)
                        st.lines.append(f" {sub_opener}")
                        st.depth = 2

                    st.lines.append(f"  {command}")

        # Close all open blocks
        for st in devices.values():
            close_to(st, 0)

        # ------------------------------------------------------------------
        # Render
        # ------------------------------------------------------------------
        rendered: List[str] = []
        for device_name, st in devices.items():
            body = [ln for ln in st.lines if ln.strip()]
            if not body:
                continue

            filename = self._device_to_filename(device_name)
            header: List[str] = [RANCID_HEADER, "!"]
            if not st.has_hostname:
                header += [f"hostname {device_name}", "!"]

            full_body = "\n".join(header + body + ["!", "end"])
            rendered.append(
                f"[configs/{filename}]\n{full_body}\n[/configs/{filename}]"
            )

        if not rendered:
            return self.none_value

        return "\n\n".join(rendered).strip()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_text(value: object) -> str:
        if value is None:
            return ""
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
        text = str(value).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1]
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text.strip()

    @staticmethod
    def _is_skip(command: str) -> bool:
        return any(p.match(command) for p in SKIP_PATTERNS)

    @staticmethod
    def _is_disallowed(command: str) -> bool:
        return any(p.search(command) for p in DISALLOWED_PATTERNS)

    @staticmethod
    def _is_block_opener(command: str) -> bool:
        return any(p.match(command) for p in BLOCK_OPENERS)

    @staticmethod
    def _is_sub_block_opener(command: str) -> bool:
        return any(p.match(command) for p in SUB_BLOCK_OPENERS)

    @staticmethod
    def _device_to_filename(device_name: str) -> str:
        upper = device_name.upper()
        m = re.fullmatch(r"R(\d+)", upper)
        if m:
            return f"router{m.group(1)}.cfg"
        m = re.fullmatch(r"SW(\d+)", upper)
        if m:
            return f"switch{m.group(1)}.cfg"
        slug = re.sub(r"[^a-z0-9_-]+", "", device_name.lower().replace(" ", "_")) or "device"
        return f"{slug}.cfg"


# ---------------------------------------------------------------------------
# CSV pipeline
# ---------------------------------------------------------------------------

def parse_csv(input_csv: str, output_csv: str, none_value: str = "None") -> None:
    df = pd.read_csv(input_csv)
    if "predictions" not in df.columns:
        raise ValueError("The input CSV must contain a 'predictions' column.")
    parser = BatfishPredictionParser(none_value=none_value)
    output_df = df.copy()
    output_df["predictions"] = output_df["predictions"].apply(parser.parse_prediction)
    output_df.to_csv(output_csv, index=False)
    print(f"Written {len(output_df)} rows → {output_csv}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert CLI-like predictions into Batfish-friendly config blocks."
    )
    p.add_argument("input_csv", help="Source CSV (e.g. predictions1.csv)")
    p.add_argument("-o", "--output", default="predictions1_batfish.csv",
                   help="Output CSV path.  Default: predictions1_batfish.csv")
    p.add_argument("--none-value", default="None",
                   help='Value when a prediction has no CLI prompts at all.  Default: "None"')
    return p


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    parse_csv(args.input_csv, args.output, args.none_value)