"""
Main discovery orchestrator.

Usage:
    from gcp_observability.discovery.discover import run_discovery
    inventory = run_discovery(["project-a", "project-b"])
"""
import json
from dataclasses import asdict

from google.cloud import run_v2

from .load_balancer import discover_lb_routes
from .cloud_run import discover_cloud_run_services
from .models import Inventory, ProjectInventory, RoutingRule


def _enrich_cloud_run_urls(rules: list[RoutingRule]) -> None:
    """
    Two cases handled here:

    1. Service-based NEG: cloud_run_service is set, cloud_run_url is None.
       Look up the specific Cloud Run service to get its *.run.app URL.

    2. URL-mask NEG: url_mask is set, service is unknown.
       List ALL Cloud Run services in that NEG's project/region — they are all
       potential backends behind the mask.

    Groups API calls by (project, region) to avoid redundant requests.
    """
    client = run_v2.ServicesClient()

    # --- Case 1: service-based NEGs ---
    service_lookups: dict[tuple[str, str, str], list[RoutingRule]] = {}
    for rule in rules:
        if rule.cloud_run_service and rule.cloud_run_url is None and rule.neg_project and rule.neg_region:
            key = (rule.neg_project, rule.neg_region, rule.cloud_run_service)
            service_lookups.setdefault(key, []).append(rule)

    for (project, region, svc_name), affected_rules in service_lookups.items():
        resource = f"projects/{project}/locations/{region}/services/{svc_name}"
        try:
            svc = client.get_service(name=resource)
            url = svc.uri
        except Exception as e:
            print(f"  [warn] Could not resolve Cloud Run URL for {svc_name} in {project}/{region}: {e}")
            url = None
        for rule in affected_rules:
            rule.cloud_run_url = url

    # --- Case 2: url_mask NEGs — list all services in that project/region ---
    mask_lookups: dict[tuple[str, str], list[RoutingRule]] = {}
    for rule in rules:
        if rule.url_mask and rule.neg_project and rule.neg_region:
            key = (rule.neg_project, rule.neg_region)
            mask_lookups.setdefault(key, []).append(rule)

    for (project, region), affected_rules in mask_lookups.items():
        parent = f"projects/{project}/locations/{region}"
        try:
            services = [
                {"name": s.name.split("/")[-1], "url": s.uri}
                for s in client.list_services(parent=parent)
            ]
        except Exception as e:
            print(f"  [warn] Could not list Cloud Run services for url_mask in {project}/{region}: {e}")
            services = []
        for rule in affected_rules:
            rule.url_mask_services = services


def run_discovery(project_ids: list[str]) -> Inventory:
    inventory = Inventory()

    for project_id in project_ids:
        print(f"\n[{project_id}] Scanning...")
        proj_inv = ProjectInventory(project_id=project_id)

        print(f"  → Load balancer routes")
        proj_inv.routing_rules = discover_lb_routes(project_id)

        print(f"  → Enriching Cloud Run URLs")
        _enrich_cloud_run_urls(proj_inv.routing_rules)

        print(f"  → Cloud Run services")
        all_cr_services = discover_cloud_run_services(project_id)

        # Mark which Cloud Run services are already covered by the LB
        lb_backed = {r.cloud_run_service for r in proj_inv.routing_rules if r.cloud_run_service}
        proj_inv.standalone_cloud_run = [s for s in all_cr_services if s["name"] not in lb_backed]

        inventory.projects.append(proj_inv)

    return inventory


def print_inventory(inventory: Inventory) -> None:
    for proj in inventory.projects:
        print(f"\n{'='*60}")
        print(f"Project: {proj.project_id}")
        print(f"{'='*60}")

        if proj.routing_rules:
            print("\n  Load Balancer Routes:")
            for r in proj.routing_rules:
                print(f"\n    https://{r.host}{r.path}")
                if r.cloud_run_service:
                    loc = f" (project: {r.neg_project}, region: {r.neg_region})"
                    print(f"      → Cloud Run: {r.cloud_run_service}{loc}")
                    if r.cloud_run_url:
                        print(f"         {r.cloud_run_url}")
                elif r.url_mask:
                    print(f"      → Cloud Run URL mask: {r.url_mask}")
                    for s in r.url_mask_services:
                        print(f"         {s['name']}  {s['url']}")
                elif r.app_engine_service:
                    print(f"      → App Engine: {r.app_engine_service}")
                    if r.app_engine_url:
                        print(f"         {r.app_engine_url}")
                else:
                    print(f"      → Backend: {r.backend_service} (backend type unknown)")
        else:
            print("\n  No load balancer routes found.")

        if proj.standalone_cloud_run:
            print("\n  Standalone Cloud Run (not behind LB):")
            for s in proj.standalone_cloud_run:
                print(f"    [{s['region']}] {s['name']}")
                print(f"      {s['url']}  (ingress: {s['ingress']})")


def inventory_to_json(inventory: Inventory) -> str:
    return json.dumps(asdict(inventory), indent=2)
