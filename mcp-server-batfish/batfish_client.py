from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

try:
    from pybatfish.client.session import Session
except ImportError as exc:
    raise ImportError(
        "pybatfish is not installed. Install it with: pip install pybatfish"
    ) from exc


class BatfishClient:
    """
    Minimal Batfish service layer compatible with the simplified server.py.

    Responsibilities:
    - Receive an already extracted snapshot path
    - Initialize the snapshot in Batfish and keep it active
    - Execute Snapshot Input questions:
        - fileParseStatus
        - parseWarning
        - initIssues
    - Transform Batfish DataFrames into plain dicts
    """

    def __init__(
        self,
        host: str | None = None,
        network_name: str | None = None,
        snapshot_name: str | None = None,
    ) -> None:
        self.host = host or os.getenv("BATFISH_HOST", "localhost")
        self.network_name = network_name or os.getenv(
            "BATFISH_NETWORK", "mcp_batfish_network"
        )
        self.snapshot_name = snapshot_name or os.getenv(
            "BATFISH_SNAPSHOT", "mcp_active_snapshot"
        )

        self.session = Session(host=self.host)

        self._active_snapshot_loaded = False
        self._active_snapshot_root: Path | None = None
        self._current_snapshot_name: str | None = None
        self._parse_failed = False
        self._parse_error = ""
        self._config_files: list[str] = []

    # =========================
    # Public API
    # =========================

    def load_snapshot(self, snapshot_path: str) -> dict[str, Any]:
        """
        Initialize an already extracted snapshot directory in Batfish
        and keep it as the active snapshot.
        """
        snapshot_root = Path(snapshot_path)

        if not snapshot_root.exists():
            raise FileNotFoundError(f"Snapshot path does not exist: {snapshot_path}")

        if not snapshot_root.is_dir():
            raise ValueError(f"Snapshot path is not a directory: {snapshot_path}")

        configs_dir = snapshot_root / "configs"
        if not configs_dir.exists() or not configs_dir.is_dir():
            raise ValueError(
                f"Snapshot path does not contain required 'configs/' directory: {snapshot_path}"
            )

        self.session.set_network(self.network_name)

        # Batfish leaves a snapshot in PARSING_FAIL when its parsing work dies, and it
        # then refuses to queue any question against that name while never moving the
        # work to a terminal status, so pybatfish polls forever. Never reuse a name.
        snapshot_name = f"{self.snapshot_name}_{uuid.uuid4().hex[:12]}"

        if self._current_snapshot_name is not None:
            try:
                self.session.delete_snapshot(self._current_snapshot_name)
            except Exception:
                pass

        # Match the "configs/<name>" shape Batfish itself reports in fileParseStatus.
        self._config_files = sorted(
            f"configs/{path.name}" for path in configs_dir.iterdir() if path.is_file()
        )
        self._parse_failed = False
        self._parse_error = ""

        try:
            self.session.init_snapshot(
                str(snapshot_root),
                name=snapshot_name,
                overwrite=True,
            )
        except Exception as exc:
            # A config Batfish cannot parse at all is a legitimate FAILED result,
            # not a reason to abort the run.
            self._parse_failed = True
            self._parse_error = str(exc)

        self._current_snapshot_name = snapshot_name
        self._active_snapshot_root = snapshot_root
        self._active_snapshot_loaded = True

        return {
            "active": True,
            "snapshot_root": str(snapshot_root),
            "network_name": self.network_name,
            "snapshot_name": snapshot_name,
            "parse_failed": self._parse_failed,
        }

    def file_parse_status(self) -> dict[str, Any]:
        """
        Run fileParseStatus on the active snapshot and return normalized results.
        """
        self._ensure_active_snapshot()

        if self._parse_failed:
            results = [
                {
                    "file_name": file_name,
                    "status": "FAILED",
                    "file_format": "",
                    "nodes": [],
                    "error": self._parse_error,
                }
                for file_name in self._config_files
            ]
            return {
                "results": results,
                "summary": {
                    "passed": 0,
                    "failed": len(results),
                    "partially_parsed": 0,
                },
            }

        df = self.session.q.fileParseStatus().answer().frame()

        results: list[dict[str, Any]] = []
        passed = 0
        failed = 0
        partially_parsed = 0

        for _, row in df.iterrows():
            file_name = self._as_str(row.get("File_Name"))
            status = self._as_str(row.get("Status")).upper()
            file_format = self._as_str(row.get("File_Format"))
            nodes = self._as_list_of_str(row.get("Nodes"))

            results.append(
                {
                    "file_name": file_name,
                    "status": status,
                    "file_format": file_format,
                    "nodes": nodes,
                }
            )

            if status == "PASSED":
                passed += 1
            elif status == "FAILED":
                failed += 1
            elif status in {"PARTIALLY_PARSED", "PARTIALLY_UNRECOGNIZED"}:
                partially_parsed += 1

        return {
            "results": results,
            "summary": {
                "passed": passed,
                "failed": failed,
                "partially_parsed": partially_parsed,
            },
        }

    def parse_warning(self, aggregate_duplicates: bool = True) -> dict[str, Any]:
        """
        Run parseWarning on the active snapshot and return normalized results.
        """
        self._ensure_active_snapshot()

        if self._parse_failed:
            return {"results": [], "summary": {"total_warnings": 0}}

        df = self.session.q.parseWarning(
            aggregateDuplicates=aggregate_duplicates
        ).answer().frame()

        results: list[dict[str, Any]] = []

        for _, row in df.iterrows():
            results.append(
                {
                    "filename": self._as_str(row.get("Filename")),
                    "line": self._as_int(row.get("Line"), default=1),
                    "text": self._as_str(row.get("Text")),
                    "parser_context": self._as_str(row.get("Parser_Context")),
                    "comment": self._as_str(row.get("Comment")),
                }
            )

        return {
            "results": results,
            "summary": {
                "total_warnings": len(results),
            },
        }

    def init_issues(self) -> dict[str, Any]:
        """
        Run initIssues on the active snapshot and return normalized results.
        """
        self._ensure_active_snapshot()

        if self._parse_failed:
            return {"results": [], "summary": {"total_issues": 0}}

        df = self.session.q.initIssues().answer().frame()

        results: list[dict[str, Any]] = []

        for _, row in df.iterrows():
            results.append(
                {
                    "nodes": self._as_list_of_str(row.get("Nodes")),
                    "source_lines": self._as_list_of_str(row.get("Source_Lines")),
                    "type": self._as_str(row.get("Type")),
                    "details": self._as_str(row.get("Details")),
                    "line_text": self._as_str(row.get("Line_Text")),
                    "parser_context": self._as_str(row.get("Parser_Context")),
                }
            )

        return {
            "results": results,
            "summary": {
                "total_issues": len(results),
            },
        }

    # =========================
    # Internal helpers
    # =========================

    def _ensure_active_snapshot(self) -> None:
        if not self._active_snapshot_loaded:
            raise RuntimeError(
                "No active snapshot loaded. Call 'load_snapshot' first."
            )

    @staticmethod
    def _is_nullish(value: Any) -> bool:
        if value is None:
            return True
        try:
            return bool(value != value)
        except Exception:
            return False

    def _as_str(self, value: Any) -> str:
        if self._is_nullish(value):
            return ""
        return str(value)

    def _as_int(self, value: Any, default: int = 0) -> int:
        if self._is_nullish(value):
            return default
        try:
            return int(value)
        except Exception:
            return default

    def _as_list_of_str(self, value: Any) -> list[str]:
        if self._is_nullish(value):
            return []

        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value]

        return [str(value)]