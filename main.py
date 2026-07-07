"""
gcp-observability CLI

Usage:
    uv run main.py discover --projects=proj-a,proj-b
    uv run main.py discover --projects=proj-a,proj-b --output=inventory.json
"""
import argparse
import sys

from gcp_observability.discovery.discover import run_discovery, print_inventory, inventory_to_json


def cmd_discover(args):
    project_ids = [p.strip() for p in args.projects.split(",") if p.strip()]
    if not project_ids:
        print("Error: --projects must be a comma-separated list of GCP project IDs")
        sys.exit(1)

    print(f"Discovering APIs across projects: {', '.join(project_ids)}")
    inventory = run_discovery(project_ids)
    print_inventory(inventory)

    if args.output:
        with open(args.output, "w") as f:
            f.write(inventory_to_json(inventory))
        print(f"\nInventory written to {args.output}")


def main():
    parser = argparse.ArgumentParser(description="GCP API Observability Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover", help="Discover all API routes across projects")
    discover_parser.add_argument(
        "--projects",
        required=True,
        help="Comma-separated list of GCP project IDs to scan",
    )
    discover_parser.add_argument(
        "--output",
        help="Optional path to write JSON inventory (e.g. inventory.json)",
    )

    args = parser.parse_args()

    if args.command == "discover":
        cmd_discover(args)


if __name__ == "__main__":
    main()
