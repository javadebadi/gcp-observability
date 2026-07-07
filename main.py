"""
gcp-observability CLI

Usage:
    uv run main.py discover --projects=proj-a,proj-b [--output=inventory.json]
    uv run main.py trace-coverage --inventory=inventory.json [--hours=24] [--sample=20]
    uv run main.py trace-coverage --projects=proj-a,proj-b [--hours=24] [--sample=20]
"""
import argparse
import json
import sys

from gcp_observability.discovery.discover import run_discovery, print_inventory, inventory_to_json
from gcp_observability.discovery.models import Inventory
from gcp_observability.discovery.cloud_run import discover_cloud_run_services
from gcp_observability.logs.trace_coverage import check_trace_coverage, ServiceTraceCoverage


VERDICT_ICON = {
    "OK":      "✓",
    "PARTIAL": "~",
    "NO_TRACE": "!",
    "DARK":    "✗",
    "NO_DATA": "?",
}


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


def _load_or_discover(args) -> Inventory:
    if hasattr(args, "inventory") and args.inventory:
        with open(args.inventory) as f:
            data = json.load(f)
        # Rebuild dataclasses from JSON
        from gcp_observability.discovery.models import ProjectInventory, RoutingRule
        inventory = Inventory()
        for p in data["projects"]:
            proj = ProjectInventory(
                project_id=p["project_id"],
                standalone_cloud_run=p.get("standalone_cloud_run", []),
                standalone_app_engine=p.get("standalone_app_engine", []),
            )
            for r in p.get("routing_rules", []):
                proj.routing_rules.append(RoutingRule(**r))
            inventory.projects.append(proj)
        return inventory

    project_ids = [p.strip() for p in args.projects.split(",") if p.strip()]
    print(f"Running discovery first across: {', '.join(project_ids)}")
    return run_discovery(project_ids)


def cmd_trace_coverage(args):
    # Collect unique (project, service, region) → list of known URLs
    services: dict[tuple[str, str, str], list[str]] = {}

    if hasattr(args, "inventory") and args.inventory:
        # Load from a previously saved inventory — includes LB route context
        inventory = _load_or_discover(args)
        for proj in inventory.projects:
            for rule in proj.routing_rules:
                if rule.cloud_run_service and rule.neg_project and rule.neg_region:
                    key = (rule.neg_project, rule.cloud_run_service, rule.neg_region)
                    route = f"https://{rule.host}{rule.path}"
                    services.setdefault(key, []).append(route)
            for svc in proj.standalone_cloud_run:
                key = (proj.project_id, svc["name"], svc["region"])
                services.setdefault(key, []).append(svc["url"])
    else:
        # Direct Cloud Run discovery — no LB traversal needed for log analysis
        project_ids = [p.strip() for p in args.projects.split(",") if p.strip()]
        for project_id in project_ids:
            print(f"[{project_id}] Listing Cloud Run services...")
            for svc in discover_cloud_run_services(project_id):
                key = (project_id, svc["name"], svc["region"])
                services.setdefault(key, []).append(svc["url"])

    if not services:
        print("No Cloud Run services found in inventory.")
        return

    print(f"\nChecking trace coverage for {len(services)} service(s) "
          f"(last {args.hours}h, {args.sample} request sample each)...\n")

    results: list[tuple[ServiceTraceCoverage, list[str]]] = []
    for (project, service, region), routes in services.items():
        print(f"  [{project}] {service} ({region})...")
        coverage = check_trace_coverage(
            project=project,
            service=service,
            region=region,
            hours_back=args.hours,
            sample_size=args.sample,
        )
        results.append((coverage, routes))

    _print_trace_coverage_report(results)


def _print_trace_coverage_report(results: list[tuple[ServiceTraceCoverage, list[str]]]) -> None:
    ok = [r for r, _ in results if r.verdict == "OK"]

    print("\n" + "="*65)
    print("  TRACE COVERAGE REPORT")
    print(f"  {len(ok)}/{len(results)} services fully instrumented")
    print("="*65)

    # Group by verdict so gaps are front and centre
    for verdict_filter, label in [
        (["DARK", "NO_TRACE", "PARTIAL", "NO_DATA"], "GAPS / NEEDS ATTENTION"),
        (["OK"], "OK"),
    ]:
        filtered = [(cov, routes) for cov, routes in results if cov.verdict in verdict_filter]
        if not filtered:
            continue
        print(f"\n  {label}")
        print("  " + "-"*60)
        for cov, routes in filtered:
            icon = VERDICT_ICON.get(cov.verdict, "?")
            print(f"\n  {icon} [{cov.verdict}] {cov.service}  ({cov.project} / {cov.region})")
            for route in routes:
                print(f"      route: {route}")
            if cov.sample_requests == 0:
                print("      no requests in the last window — service may be idle")
            else:
                pct = int(100 * cov.app_logs_with_trace / cov.sample_requests)
                print(f"      sampled {cov.sample_requests} requests → "
                      f"{cov.app_logs_with_trace} with trace ({pct}%)")
                if cov.app_logs_without_trace:
                    print(f"      {cov.app_logs_without_trace} requests had app logs but NO trace field")
                if cov.no_app_logs:
                    print(f"      {cov.no_app_logs} requests had zero app log correlation (DARK)")
            if cov.example_missing_trace_ids:
                print("      example trace IDs to inspect:")
                for tid in cov.example_missing_trace_ids:
                    print(f"        {tid}")

    print()


def main():
    parser = argparse.ArgumentParser(description="GCP API Observability Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # discover
    discover_parser = subparsers.add_parser("discover", help="Discover all API routes across projects")
    discover_parser.add_argument("--projects", required=True,
                                 help="Comma-separated GCP project IDs")
    discover_parser.add_argument("--output", help="Write JSON inventory to this path")

    # trace-coverage
    tc_parser = subparsers.add_parser(
        "trace-coverage",
        help="Find Cloud Run services missing trace IDs in their application logs",
    )
    tc_source = tc_parser.add_mutually_exclusive_group(required=True)
    tc_source.add_argument("--inventory", help="Path to inventory.json from 'discover'")
    tc_source.add_argument("--projects", help="Comma-separated GCP project IDs (runs discovery first)")
    tc_parser.add_argument("--hours", type=int, default=24,
                           help="Look back this many hours in logs (default: 24)")
    tc_parser.add_argument("--sample", type=int, default=20,
                           help="Number of recent requests to sample per service (default: 20)")

    args = parser.parse_args()

    if args.command == "discover":
        cmd_discover(args)
    elif args.command == "trace-coverage":
        cmd_trace_coverage(args)


if __name__ == "__main__":
    main()
