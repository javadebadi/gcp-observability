from dataclasses import dataclass, field


@dataclass
class RoutingRule:
    """One resolved external path → backend mapping.

    cloud_run_service + cloud_run_url: set when the NEG points to a named service.
      cloud_run_url is filled in by the enrichment step (Cloud Run API lookup).

    url_mask: set when the NEG uses a URL mask pattern like <service>-hash.run.app.
      In that case cloud_run_service/url are None — the mask itself IS the routing rule.

    app_engine_service + app_engine_url: set when the NEG points to App Engine.
    """
    host: str
    path: str
    backend_service: str
    lb_project: str
    url_map: str
    neg_name: str | None = None
    neg_project: str | None = None
    neg_region: str | None = None
    # Cloud Run — service-based NEG
    cloud_run_service: str | None = None
    cloud_run_url: str | None = None
    # Cloud Run — url_mask-based NEG (pattern, not a real URL)
    url_mask: str | None = None
    # Services resolved from url_mask: all Cloud Run services in the NEG's project/region
    url_mask_services: list[dict] = None  # [{"name": ..., "url": ...}, ...]

    def __post_init__(self):
        if self.url_mask_services is None:
            self.url_mask_services = []
    # App Engine
    app_engine_service: str | None = None
    app_engine_url: str | None = None


@dataclass
class ProjectInventory:
    project_id: str
    routing_rules: list[RoutingRule] = field(default_factory=list)
    standalone_cloud_run: list[dict] = field(default_factory=list)
    standalone_app_engine: list[dict] = field(default_factory=list)


@dataclass
class Inventory:
    projects: list[ProjectInventory] = field(default_factory=list)
