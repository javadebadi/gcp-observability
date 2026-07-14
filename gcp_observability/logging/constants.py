"""Cloud Logging constants: severity levels, resource types, and payload types."""

from __future__ import annotations

from enum import StrEnum


class PayloadType(StrEnum):
    TEXT = "text"
    JSON = "json"
    PROTO = "proto"


class Severity:
    DEFAULT = "DEFAULT"
    DEBUG = "DEBUG"
    INFO = "INFO"
    NOTICE = "NOTICE"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    ALERT = "ALERT"
    EMERGENCY = "EMERGENCY"

    # Ordered list (lowest → highest) for range helpers
    _LEVELS = [DEFAULT, DEBUG, INFO, NOTICE, WARNING, ERROR, CRITICAL, ALERT, EMERGENCY]


class ResourceType:
    GLOBAL = "global"
    GCE_INSTANCE = "gce_instance"
    GKE_CONTAINER = "k8s_container"
    GKE_POD = "k8s_pod"
    GKE_NODE = "k8s_node"
    GKE_CLUSTER = "k8s_cluster"
    CLOUD_RUN_REVISION = "cloud_run_revision"
    CLOUD_RUN_JOB = "cloud_run_job"
    CLOUD_FUNCTION = "cloud_function"
    APP_ENGINE = "gae_app"
    CLOUD_SQL = "cloudsql_database"
    LOAD_BALANCER = "http_load_balancer"
    PUBSUB_TOPIC = "pubsub_topic"
    PUBSUB_SUBSCRIPTION = "pubsub_subscription"
    BIG_QUERY = "bigquery_resource"
    STORAGE = "gcs_bucket"
    DATAFLOW = "dataflow_step"
    CLOUD_TASKS_QUEUE = "cloud_tasks_queue"
    REDIS_INSTANCE = "redis_instance"
