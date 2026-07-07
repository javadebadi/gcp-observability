"""Discover Cloud Run services across a GCP project."""
from google.cloud import run_v2


def discover_cloud_run_services(project_id: str) -> list[dict]:
    """
    Return all Cloud Run services in the project with their URLs.
    This covers both LB-fronted and standalone (direct) services.
    """
    client = run_v2.ServicesClient()
    parent = f"projects/{project_id}/locations/-"  # '-' means all regions

    services = []
    try:
        for svc in client.list_services(parent=parent):
            name_parts = svc.name.split("/")
            # projects/{proj}/locations/{region}/services/{name}
            region = name_parts[3] if len(name_parts) > 3 else "unknown"
            svc_name = name_parts[-1]

            services.append(
                {
                    "project": project_id,
                    "name": svc_name,
                    "region": region,
                    "url": svc.uri,
                    "ingress": svc.ingress.name if svc.ingress else None,
                }
            )
    except Exception as e:
        print(f"  [warn] Could not list Cloud Run services in {project_id}: {e}")

    return services
