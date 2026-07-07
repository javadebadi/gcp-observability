"""Discover Cloud Run services across a GCP project."""
from google.cloud import run_v2


def _list_available_regions(client: run_v2.ServicesClient, project_id: str) -> list[str]:
    """
    List the Cloud Run regions available in the project using the Locations API.
    Used as fallback when the '-' wildcard doesn't return results.
    """
    try:
        pager = client.list_locations(request={"name": f"projects/{project_id}"})
        return [loc.name.split("/")[-1] for loc in pager]
    except Exception as e:
        print(f"  [warn] Could not list Cloud Run regions in {project_id}: {e}")
        return []


def _services_in_region(
    client: run_v2.ServicesClient, project_id: str, region: str
) -> list[dict]:
    services = []
    try:
        parent = f"projects/{project_id}/locations/{region}"
        for svc in client.list_services(parent=parent):
            parts = svc.name.split("/")
            services.append({
                "project": project_id,
                "name": parts[-1],
                "region": region,
                "url": svc.uri,
                "ingress": svc.ingress.name if svc.ingress else None,
            })
    except Exception as e:
        print(f"  [warn] Could not list services in {project_id}/{region}: {e}")
    return services


def discover_cloud_run_services(project_id: str) -> list[dict]:
    """
    Return all Cloud Run services in the project with their URLs.

    Tries the '-' wildcard first (one API call). If that returns nothing,
    falls back to listing available regions explicitly and querying each.
    """
    client = run_v2.ServicesClient()
    services = []

    # Attempt 1: wildcard across all regions
    try:
        for svc in client.list_services(parent=f"projects/{project_id}/locations/-"):
            parts = svc.name.split("/")
            region = parts[3] if len(parts) > 3 else "unknown"
            services.append({
                "project": project_id,
                "name": parts[-1],
                "region": region,
                "url": svc.uri,
                "ingress": svc.ingress.name if svc.ingress else None,
            })
    except Exception as e:
        print(f"  [warn] Wildcard listing failed for {project_id}: {e}")

    if services:
        return services

    # Attempt 2: enumerate regions then query each
    print(f"  [info] Wildcard returned 0 results for {project_id}, trying per-region...")
    regions = _list_available_regions(client, project_id)
    if not regions:
        print(f"  [warn] No Cloud Run regions found in {project_id} — check permissions")
        return []

    print(f"  [info] Found {len(regions)} region(s): {', '.join(regions)}")
    for region in regions:
        services.extend(_services_in_region(client, project_id, region))

    if not services:
        print(f"  [warn] No Cloud Run services found in {project_id} across {len(regions)} region(s)")

    return services
