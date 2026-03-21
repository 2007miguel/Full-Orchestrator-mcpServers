import json
from mcp_client import start, initialize, call_tool, close

start()

try:
    init_response = initialize()

    print("\n=== load_snapshot ===")
    load_response = call_tool(
        "load_snapshot",
        {
            "zip_path": r"C:\Users\juan\Documents\snapshot.zip"
        }
    )
    print(json.dumps(load_response, indent=2, ensure_ascii=False))

    print("\n=== file_parse_status ===")
    status_response = call_tool("file_parse_status", {})
    print(json.dumps(status_response, indent=2, ensure_ascii=False))

    print("\n=== parse_warning ===")
    warning_response = call_tool(
        "parse_warning",
        {
            "aggregate_duplicates": True
        }
    )
    print(json.dumps(warning_response, indent=2, ensure_ascii=False))

    print("\n=== init_issues ===")
    issues_response = call_tool("init_issues", {})
    print(json.dumps(issues_response, indent=2, ensure_ascii=False))

finally:
    close()