"""
Traverse the GCP load balancer chain to resolve external URLs to backends.

Chain:
  ForwardingRule → TargetHTTPSProxy → URLMap → BackendService → ServerlessNEG → Cloud Run / App Engine

The domain name (e.g. example.com) comes from one of two places:
  - URL map hostRules: explicit hostnames like "example.com" or "*.example.com"
  - SSL certificate SANs: when the URL map host rule is "*" (wildcard), the real
    domain lives on the cert attached to the TargetHTTPSProxy
"""
import re
from google.cloud import compute_v1
from .models import RoutingRule


def _project_from_link(self_link: str) -> str:
    m = re.search(r"/projects/([^/]+)/", self_link)
    return m.group(1) if m else ""


def _name_from_link(self_link: str) -> str:
    return self_link.rstrip("/").split("/")[-1]


def _region_from_link(self_link: str) -> str | None:
    m = re.search(r"/regions/([^/]+)/", self_link)
    return m.group(1) if m else None


def _ssl_cert_domains(
    certs_client: compute_v1.SslCertificatesClient,
    project_id: str,
    cert_links: list[str],
) -> list[str]:
    """Extract all domain names from the SSL certificates on a proxy."""
    domains: list[str] = []
    for cert_link in cert_links:
        cert_name = _name_from_link(cert_link)
        try:
            cert = certs_client.get(project=project_id, ssl_certificate=cert_name)
            # managed certs list domains; self-managed certs use the subject/SANs
            if cert.managed and cert.managed.domains:
                domains.extend(cert.managed.domains)
            elif cert.subject_alternative_names:
                domains.extend(cert.subject_alternative_names)
        except Exception as e:
            print(f"  [warn] Could not read SSL cert {cert_name}: {e}")
    return domains


def _resolve_neg(
    negs_client: compute_v1.RegionNetworkEndpointGroupsClient,
    neg_project: str,
    neg_region: str,
    neg_name: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Returns (cloud_run_service, url_mask, app_engine_service, app_engine_version).
    Exactly one group will be non-None depending on the NEG type.
    """
    try:
        neg = negs_client.get(
            project=neg_project,
            region=neg_region,
            network_endpoint_group=neg_name,
        )
    except Exception as e:
        print(f"  [warn] Could not get NEG {neg_name} in {neg_project}/{neg_region}: {e}")
        return None, None, None, None

    if neg.cloud_run:
        if neg.cloud_run.service:
            # Service-based: one specific Cloud Run service.
            # URL resolved later via Cloud Run API.
            return neg.cloud_run.service, None, None, None
        if neg.cloud_run.url_mask:
            # Mask-based: one NEG serves many services based on the request URL.
            # The mask is the answer; services resolved later by listing Cloud Run in that project.
            return None, neg.cloud_run.url_mask, None, None

    if neg.app_engine:
        svc = neg.app_engine.service or "default"
        version = neg.app_engine.version or None
        return None, None, svc, version

    return None, None, None, None


def discover_lb_routes(project_id: str) -> list[RoutingRule]:
    """Scan one GCP project for HTTPS load balancers and return all resolved routing rules."""
    fwd_client = compute_v1.GlobalForwardingRulesClient()
    proxies_client = compute_v1.TargetHttpsProxiesClient()
    certs_client = compute_v1.SslCertificatesClient()
    url_maps_client = compute_v1.UrlMapsClient()
    backend_services_client = compute_v1.BackendServicesClient()
    negs_client = compute_v1.RegionNetworkEndpointGroupsClient()

    routing_rules: list[RoutingRule] = []

    # Step 1: forwarding rules — entry points for HTTPS load balancers
    try:
        fwd_rules = list(fwd_client.list(project=project_id))
    except Exception as e:
        print(f"  [warn] Could not list forwarding rules in {project_id}: {e}")
        return []

    https_rules = [r for r in fwd_rules if r.target and "targetHttpsProxies" in r.target]
    if not https_rules:
        return []

    for fwd_rule in https_rules:
        # Step 2: target HTTPS proxy → SSL certs + URL map
        proxy_name = _name_from_link(fwd_rule.target)
        try:
            proxy = proxies_client.get(project=project_id, target_https_proxy=proxy_name)
        except Exception as e:
            print(f"  [warn] Could not get proxy {proxy_name}: {e}")
            continue

        if not proxy.url_map:
            continue

        # SSL cert domains are the fallback when the URL map host rule is "*"
        cert_domains = _ssl_cert_domains(certs_client, project_id, list(proxy.ssl_certificates))

        url_map_name = _name_from_link(proxy.url_map)

        # Step 3: URL map → host rules + path matchers
        try:
            url_map = url_maps_client.get(project=project_id, url_map=url_map_name)
        except Exception as e:
            print(f"  [warn] Could not get url map {url_map_name}: {e}")
            continue

        # host → which path_matcher to use.
        # When host is "*", substitute with the real domains from the SSL cert.
        host_to_matcher: dict[str, str] = {}
        for hr in url_map.host_rules:
            for host in hr.hosts:
                if host == "*" and cert_domains:
                    for domain in cert_domains:
                        host_to_matcher[domain] = hr.path_matcher
                else:
                    host_to_matcher[host] = hr.path_matcher

        # path_matcher name → list of (paths, backend_service_name)
        matcher_to_rules: dict[str, list[tuple[list[str], str]]] = {}
        for pm in url_map.path_matchers:
            rules: list[tuple[list[str], str]] = []
            for pr in pm.path_rules:
                if pr.service:
                    rules.append((list(pr.paths), _name_from_link(pr.service)))
            if pm.default_service:
                rules.append((["/*"], _name_from_link(pm.default_service)))
            matcher_to_rules[pm.name] = rules

        # URL maps can have a top-level defaultService that catches everything
        # not matched by any host/path rule. Treat it as host="*" path="/*".
        if url_map.default_service:
            fallback_hosts = cert_domains if cert_domains else ["*"]
            fallback_svc = _name_from_link(url_map.default_service)
            for host in fallback_hosts:
                # Only add if this host isn't already covered by an explicit host rule
                if host not in host_to_matcher:
                    host_to_matcher[host] = f"__default__{fallback_svc}"
            matcher_to_rules[f"__default__{fallback_svc}"] = [(["/*"], fallback_svc)]

        # Step 4: for every (host, path) → backend service → NEG → Cloud Run / App Engine
        for host, matcher_name in host_to_matcher.items():
            for paths, backend_svc_name in matcher_to_rules.get(matcher_name, []):

                # Step 5: backend service → NEG group links
                try:
                    bs = backend_services_client.get(
                        project=project_id, backend_service=backend_svc_name
                    )
                except Exception as e:
                    print(f"  [warn] Could not get backend service {backend_svc_name}: {e}")
                    for path in paths:
                        routing_rules.append(RoutingRule(
                            host=host, path=path,
                            backend_service=backend_svc_name,
                            lb_project=project_id, url_map=url_map_name,
                        ))
                    continue

                # A backend service can have multiple backends (NEGs in different regions).
                # Record one RoutingRule per NEG so each region is visible.
                neg_backends = [b for b in bs.backends if b.group]
                if not neg_backends:
                    for path in paths:
                        routing_rules.append(RoutingRule(
                            host=host, path=path,
                            backend_service=backend_svc_name,
                            lb_project=project_id, url_map=url_map_name,
                        ))
                    continue

                for backend in neg_backends:
                    neg_name = _name_from_link(backend.group)
                    neg_project = _project_from_link(backend.group)
                    neg_region = _region_from_link(backend.group)

                    cr_service, url_mask, ae_service, ae_version = None, None, None, None

                    if neg_region:
                        # Step 6: resolve the NEG
                        cr_service, url_mask, ae_service, ae_version = _resolve_neg(
                            negs_client, neg_project, neg_region, neg_name
                        )
                    else:
                        print(f"  [info] Skipping non-regional NEG {neg_name} (not serverless)")

                    # App Engine URL is constructible from the service name + project
                    ae_url = None
                    if ae_service:
                        svc_part = f"{ae_service}-dot-" if ae_service != "default" else ""
                        ver_part = f"{ae_version}-dot-" if ae_version else ""
                        ae_url = f"https://{ver_part}{svc_part}{neg_project}.appspot.com"

                    for path in paths:
                        routing_rules.append(RoutingRule(
                            host=host,
                            path=path,
                            backend_service=backend_svc_name,
                            lb_project=project_id,
                            url_map=url_map_name,
                            neg_name=neg_name,
                            neg_project=neg_project,
                            neg_region=neg_region,
                            cloud_run_service=cr_service,
                            cloud_run_url=None,  # filled by enrichment step in discover.py
                            url_mask=url_mask,
                            app_engine_service=ae_service,
                            app_engine_url=ae_url,
                        ))

    return routing_rules
