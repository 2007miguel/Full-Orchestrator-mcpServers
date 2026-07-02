import json
import sys
from pathlib import Path

from rich import box
from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

# Import core components of the orchestration system
from core.execution_controller import ExecutionController
from core.prompt_manager import PromptManager
from core.requirement_loader import RequirementLoader
from models.execution_context import ExecutionContext

# Import the real MCP Client Manager
from mcp_client import MCPClientManager, SERVERS


console = Console()


NETGEN_BANNER = r"""
 _   _      _    ____            
| \ | | ___| |_ / ___| ___ _ __  
|  \| |/ _ \ __| |  _ / _ \ '_ \ 
| |\  |  __/ |_| |_| |  __/ | | |
|_| \_|\___|\__|\____|\___|_| |_|
"""


class ConsoleResultLogger:
    """
    A simple logger that prints the final execution context to the console
    and saves it to a file.
    """

    def save(self, context: ExecutionContext):
        console.rule("[bold green]Execution Finished")

        results_dir = Path(__file__).parent / "results"
        results_dir.mkdir(exist_ok=True)
        filename = results_dir / f"execution_{context.metadata.get('request_id', 'unknown')}.json"

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(context.to_dict(), f, indent=2, ensure_ascii=False)

        console.print(f"[green]Result saved to:[/] {filename}")


def load_filter_catalog(options_path: Path) -> dict:
    with open(options_path, "r", encoding="utf-8") as f:
        return json.load(f)


def visible_filter_options(options) -> list[str]:
    return sorted(option for option in options if str(option).strip().lower() != "not defined")


def render_banner() -> None:
    banner = Text(NETGEN_BANNER, style="bold cyan")
    console.print(banner)


def render_section(title: str, style: str = "cyan") -> None:
    console.print(Rule(f"[bold {style}]{title}[/]", style=style))


def choose_from_list(
    step: int,
    total_steps: int,
    title: str,
    instruction: str,
    options: list[str],
) -> str:
    while True:
        console.print(f"[bold cyan][{step}/{total_steps}][/bold cyan] {instruction}")
        table = Table(
            box=None,
            show_lines=False,
            header_style="bold cyan",
            padding=(0, 1),
        )
        table.add_column("#", justify="right", style="bold green", no_wrap=True)
        table.add_column(title, style="white")

        for index, option in enumerate(options, start=1):
            table.add_row(str(index), option)

        console.print(table)
        user_choice = console.input("[bold cyan]>[/] Select an option by number: ").strip()

        if user_choice.lower() in ["exit", "quit"]:
            raise KeyboardInterrupt

        if not user_choice.isdigit():
            console.print("[yellow]Please enter a valid number.[/]")
            continue

        selected_index = int(user_choice)
        if 1 <= selected_index <= len(options):
            return options[selected_index - 1]

        console.print("[yellow]That number is outside the available options.[/]")


def collect_device_filters(filter_catalog: dict) -> list[dict]:
    device_types = visible_filter_options(filter_catalog.keys())
    device_type = choose_from_list(
        1,
        4,
        "Device Type",
        "Choose the device type to filter the RAG search.",
        device_types,
    )

    products = visible_filter_options(filter_catalog[device_type].keys())
    product = choose_from_list(
        2,
        4,
        "Product",
        "Choose the product for the selected device type.",
        products,
    )

    operating_systems = visible_filter_options(filter_catalog[device_type][product].keys())
    operating_system = choose_from_list(
        3,
        4,
        "Operating System",
        "Choose the operating system for the selected product.",
        operating_systems,
    )

    versions = visible_filter_options(filter_catalog[device_type][product][operating_system])
    selected_filters = []

    while True:
        version = choose_from_list(
            4,
            4,
            "Version",
            "Choose the software version for the selected operating system.",
            versions,
        )
        selected_filters.append({
            "device_type": device_type,
            "product": product,
            "operating_system": operating_system,
            "version": version,
        })

        while True:
            version_action = console.input(
                "[bold cyan]>[/] Add another version for this same device? (y/n): "
            ).strip().lower()

            if version_action in ["exit", "quit"]:
                raise KeyboardInterrupt

            if version_action in ["y", "yes"]:
                break

            if version_action in ["n", "no"]:
                return selected_filters

            console.print("[yellow]Please enter y or n.[/]")


def print_selected_filters(selected_filters: list[dict]) -> None:
    if not selected_filters:
        console.print("[yellow]No RAG filters selected yet.[/]")
        return

    table = Table(
        title="Current RAG Filter Selections",
        box=box.SIMPLE,
        header_style="bold cyan",
        title_style="bold cyan",
    )
    table.add_column("#", justify="right", style="bold green", no_wrap=True)
    table.add_column("Device Type", style="white")
    table.add_column("Product", style="white")
    table.add_column("Operating System", style="white")
    table.add_column("Version", style="white")

    for index, selected_filter in enumerate(selected_filters, start=1):
        table.add_row(
            str(index),
            selected_filter["device_type"],
            selected_filter["product"],
            selected_filter["operating_system"],
            selected_filter["version"],
        )

    console.print(table)


def configure_rag_filters(filter_catalog: dict) -> list[dict]:
    selected_filters = []

    render_section("RAG Filter Configuration", "magenta")
    console.print("Select the device metadata used to scope the RAG retrieval for this session.")

    while True:
        selected_filters.extend(collect_device_filters(filter_catalog))

        render_section("Filter Summary", "green")
        print_selected_filters(selected_filters)

        while True:
            action_table = Table(
                title="Next Action",
                box=None,
                show_header=False,
                title_style="bold cyan",
                padding=(0, 1),
            )
            action_table.add_column("#", justify="right", style="bold green", no_wrap=True)
            action_table.add_column("Action", style="white")
            action_table.add_row("1", "Add another device")
            action_table.add_row("2", "Remove the previous selection and start over")
            action_table.add_row("3", "Continue to the requirement chatbot")
            console.print(action_table)

            action = console.input("[bold cyan]>[/] Select an option by number: ").strip()

            if action.lower() in ["exit", "quit"]:
                raise KeyboardInterrupt

            if action == "1":
                render_section("RAG Filter Configuration", "magenta")
                break

            if action == "2":
                selected_filters.clear()
                console.print("[yellow]Selection cleared. Starting over.[/]")
                render_section("RAG Filter Configuration", "magenta")
                break

            if action == "3":
                return selected_filters

            console.print("[yellow]Please enter 1, 2, or 3.[/]")

        if selected_filters:
            continue


def main():
    """
    Main function to set up the orchestrator and run the conversational CLI.
    """
    render_banner()

    # --- Dependency Injection Setup ---
    templates_path = Path(__file__).parent / "templates"
    filter_catalog_path = Path(__file__).parent / "options" / "lista_filtro.json"

    render_section("Startup", "cyan")
    console.print("Preparing NetGen components and MCP server sessions.")

    requirement_loader = RequirementLoader()
    prompt_manager = PromptManager(templates_path=str(templates_path))
    filter_catalog = load_filter_catalog(filter_catalog_path)

    mcp_client_manager = MCPClientManager(SERVERS)

    try:
        with console.status("[bold cyan]Starting MCP servers...[/]"):
            batfish_server = mcp_client_manager.server("batfish")
            batfish_server.start()
            batfish_server.initialize()

            flm_server = mcp_client_manager.server("flm")
            flm_server.start()
            flm_server.initialize()

        startup_table = Table(box=None, show_header=False, padding=(0, 1))
        startup_table.add_column("Server", style="bold")
        startup_table.add_column("Status")
        startup_table.add_row("Batfish", "[green]Started[/]")
        startup_table.add_row("FLM", "[green]Started[/]")
        console.print(startup_table)

    except Exception as e:
        console.print(f"[red]Error while initializing MCP servers:[/] {e}")
        mcp_client_manager.close_all()
        sys.exit(1)

    result_logger = ConsoleResultLogger()

    controller = ExecutionController(
        prompt_manager=prompt_manager,
        mcp_client=mcp_client_manager,
        result_logger=result_logger,
    )

    render_section("Topology", "blue")
    console.print("[bold]TOPOLOGY SUMMARY[/]")
    console.print()
    console.print("[bold cyan]P2P Router Links:[/]")
    console.print("[green]R1[/] e0/1 10.0.12.1/24 <--> [green]R2[/] e0/0 10.0.12.2/24   NET 10.0.12.0/24")
    console.print("[green]R1[/] e0/2 10.0.14.1/24 <--> [green]R4[/] e0/0 10.0.14.2/24   NET 10.0.14.0/24")
    console.print("[green]R2[/] e0/1 10.0.23.1/24 <--> [green]R3[/] e0/0 10.0.23.2/24   NET 10.0.23.0/24")
    console.print("[green]R2[/] e0/2 10.0.24.1/24 <--> [green]R4[/] e0/1 10.0.24.2/24   NET 10.0.24.0/24")
    console.print("[green]R3[/] e0/1 10.0.34.1/24 <--> [green]R4[/] e0/2 10.0.34.2/24   NET 10.0.34.0/24")
    console.print()
    console.print("[bold cyan]LAN Segments:[/]")
    console.print("[green]h1[/] 10.0.1.10/24  GW 10.0.1.1  --> [green]SW1[/] fa0/1")
    console.print("[green]SW1[/] fa0/24 <--> [green]R1[/] e0/0 10.0.1.1/24              NET 10.0.1.0/24")
    console.print()
    console.print("[green]h2[/] 10.0.2.10/24  GW 10.0.2.1  --> [green]SW2[/] fa0/1")
    console.print("[green]SW2[/] fa0/24 <--> [green]R3[/] e0/2 10.0.2.1/24              NET 10.0.2.0/24")
    console.print()
    console.print("[bold cyan]Switch Ports:[/]")
    console.print("[green]SW1[/] fa0/1 access VLAN10 | fa0/24 to [green]R1[/]")
    console.print("[green]SW2[/] fa0/1 access VLAN30 | fa0/24 to [green]R3[/]")

    session_rag_filters = configure_rag_filters(filter_catalog)

    render_section("Chatbot", "cyan")
    console.print("Enter your network requirement. Type 'exit' or 'quit' to leave.")

    # --- Conversational Loop ---
    try:
        while True:
            user_input = console.input("\n[bold cyan]>[/] ")

            if user_input.lower() in ["exit", "quit"]:
                console.print("[cyan]Exiting orchestrator.[/]")
                break

            if not user_input.strip():
                continue

            requirement = requirement_loader.load(user_input)
            requirement["rag_filters"] = session_rag_filters
            console.print(f"[cyan]Requirement loaded[/] ID: {requirement['request_id']}")

            console.print("[cyan]Processing request: normalizing, retrieving, generating, and verifying...[/]")
            final_context = controller.run(requirement)
            model_response = getattr(final_context, "generated_config", "") or final_context.final_result

            if model_response:
                console.print("[bold cyan]Model response[/]")
                console.print(str(model_response))

            if final_context.is_success():
                console.print("[green]Requirement processed successfully![/]")
                console.print("[cyan]You can enter another requirement in the next prompt, or type 'exit' to leave.[/]")
            else:
                console.print(f"[red]Execution failed.[/] State: {final_context.state.value}")
                if final_context.error_message:
                    console.print(f"[red]Error:[/] {final_context.error_message}")

    except ValueError as e:
        console.print(f"[red]Error:[/] {e}")
    except KeyboardInterrupt:
        console.print("\n[cyan]Exiting orchestrator by user interrupt.[/]")
    except Exception as e:
        console.print(f"[red]An unexpected error occurred:[/] {e}")
    finally:
        console.print("[cyan]Closing MCP servers...[/]")
        mcp_client_manager.close_all()


if __name__ == "__main__":
    main()
