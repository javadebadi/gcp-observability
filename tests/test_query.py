from datetime import datetime, timezone


from gcp_observability.logging import F, QueryBuilder, ResourceType, Severity
from gcp_observability.logging.query import _to_iso


class TestToIso:
    def test_string_passthrough(self):
        assert _to_iso("2024-06-01T00:00:00Z") == "2024-06-01T00:00:00Z"

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime(2024, 6, 1, 12, 30, 0)
        assert _to_iso(dt) == "2024-06-01T12:30:00Z"

    def test_aware_datetime_utc(self):
        dt = datetime(2024, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        assert _to_iso(dt) == "2024-06-01T12:30:00Z"

    def test_plus_offset_normalized_to_z(self):
        from datetime import timezone, timedelta

        # +00:00 offset should come out as Z
        dt = datetime(2024, 6, 1, tzinfo=timezone(timedelta(0)))
        assert _to_iso(dt).endswith("Z")
        assert "+00:00" not in _to_iso(dt)


class TestQueryBuilderEmpty:
    def test_empty_build(self):
        assert QueryBuilder().build() == ""

    def test_str_equals_build(self):
        q = QueryBuilder().severity_gte(Severity.ERROR)
        assert str(q) == q.build()


class TestResource:
    def test_resource_type(self):
        q = QueryBuilder().resource_type(ResourceType.CLOUD_RUN_REVISION).build()
        assert q == "resource.type=cloud_run_revision"

    def test_resource_label(self):
        q = QueryBuilder().resource_label("service_name", "my-api").build()
        assert q == 'resource.labels."service_name"="my-api"'

    def test_resource_type_and_label(self):
        q = (
            QueryBuilder()
            .resource_type(ResourceType.GKE_CONTAINER)
            .resource_label("cluster_name", "prod")
            .build()
        )
        lines = q.splitlines()
        assert lines[0] == "resource.type=k8s_container"
        assert lines[1] == 'resource.labels."cluster_name"=prod'


class TestLogName:
    def test_log_name_exact(self):
        q = QueryBuilder().log_name("projects/my-project/logs/app").build()
        assert q == 'logName="projects/my-project/logs/app"'

    def test_project_with_log_id(self):
        q = QueryBuilder().project("my-project", "app").build()
        assert q == 'logName="projects/my-project/logs/app"'

    def test_project_without_log_id(self):
        q = QueryBuilder().project("my-project").build()
        assert q == 'logName:"projects/my-project/"'


class TestSeverity:
    def test_severity_eq(self):
        assert QueryBuilder().severity_eq(Severity.ERROR).build() == "severity=ERROR"

    def test_severity_gte(self):
        assert (
            QueryBuilder().severity_gte(Severity.WARNING).build() == "severity>=WARNING"
        )

    def test_severity_lte(self):
        assert QueryBuilder().severity_lte(Severity.INFO).build() == "severity<=INFO"

    def test_severity_range(self):
        q = QueryBuilder().severity_range(Severity.WARNING, Severity.ERROR).build()
        assert q == "severity>=WARNING\nseverity<=ERROR"

    def test_severity_explicit_op(self):
        assert (
            QueryBuilder().severity("!=", Severity.DEBUG).build() == "severity!=DEBUG"
        )


class TestTimeRange:
    def test_string_start_and_end(self):
        q = (
            QueryBuilder()
            .time_range("2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z")
            .build()
        )
        assert (
            q == 'timestamp>="2024-06-01T00:00:00Z"\ntimestamp<"2024-06-02T00:00:00Z"'
        )

    def test_datetime_start_and_end(self):
        q = (
            QueryBuilder()
            .time_range(
                datetime(2024, 6, 1),
                datetime(2024, 6, 2),
            )
            .build()
        )
        assert (
            q == 'timestamp>="2024-06-01T00:00:00Z"\ntimestamp<"2024-06-02T00:00:00Z"'
        )

    def test_open_ended_no_end(self):
        q = QueryBuilder().time_range("2024-06-01T00:00:00Z").build()
        assert q == 'timestamp>="2024-06-01T00:00:00Z"'

    def test_end_is_exclusive(self):
        q = (
            QueryBuilder()
            .time_range("2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z")
            .build()
        )
        assert 'timestamp<"2024-06-02T00:00:00Z"' in q
        assert 'timestamp<="2024-06-02' not in q

    def test_mixed_string_and_datetime(self):
        q = (
            QueryBuilder()
            .time_range(
                "2024-06-01T00:00:00Z",
                datetime(2024, 6, 2, tzinfo=timezone.utc),
            )
            .build()
        )
        assert 'timestamp>="2024-06-01T00:00:00Z"' in q
        assert 'timestamp<"2024-06-02T00:00:00Z"' in q


class TestSince:
    def test_since_hours_produces_timestamp_filter(self):
        q = QueryBuilder().since(hours=1).build()
        assert q.startswith('timestamp>="')
        assert q.endswith('Z"')

    def test_since_minutes(self):
        q = QueryBuilder().since(minutes=30).build()
        assert q.startswith('timestamp>="')

    def test_since_days(self):
        q = QueryBuilder().since(days=7).build()
        assert q.startswith('timestamp>="')


class TestPayload:
    def test_text_payload_has(self):
        q = QueryBuilder().text_payload("ValueError: Bad").build()
        assert q == 'textPayload:"ValueError: Bad"'

    def test_text_payload_exact(self):
        q = QueryBuilder().text_payload("exact match", exact=True).build()
        assert q == 'textPayload="exact match"'

    def test_json_payload(self):
        q = QueryBuilder().json_payload("statusCode", ">=", 500).build()
        assert q == "jsonPayload.statusCode>=500"

    def test_json_payload_has(self):
        q = QueryBuilder().json_payload_has("message", "timeout").build()
        assert q == "jsonPayload.message:timeout"

    def test_proto_payload(self):
        q = QueryBuilder().proto_payload("methodName", "=", "SetIamPolicy").build()
        assert q == "protoPayload.methodName=SetIamPolicy"


class TestHttpRequest:
    def test_http_method(self):
        assert (
            QueryBuilder().http_method("get").build() == "httpRequest.requestMethod=GET"
        )

    def test_http_status(self):
        assert (
            QueryBuilder().http_status(">=", 500).build() == "httpRequest.status>=500"
        )

    def test_http_url_has(self):
        assert (
            QueryBuilder().http_url("/api/v1").build()
            == 'httpRequest.requestUrl:"/api/v1"'
        )

    def test_http_url_exact(self):
        assert (
            QueryBuilder().http_url("/health", exact=True).build()
            == 'httpRequest.requestUrl="/health"'
        )

    def test_http_latency_gte(self):
        assert (
            QueryBuilder().http_latency_gte(2.5).build()
            == 'httpRequest.latency>="2.5s"'
        )


class TestLabels:
    def test_simple_label(self):
        assert QueryBuilder().label("env", "prod").build() == 'labels."env"=prod'

    def test_label_with_special_chars(self):
        q = QueryBuilder().label("k8s-pod/app", "my-service").build()
        assert q == 'labels."k8s-pod/app"="my-service"'


class TestTrace:
    def test_trace_has(self):
        assert QueryBuilder().trace("abc123").build() == "trace:abc123"

    def test_trace_exact(self):
        assert QueryBuilder().trace("abc123", exact=True).build() == "trace=abc123"

    def test_span_id(self):
        assert QueryBuilder().span_id("span456").build() == "spanId=span456"

    def test_sampled_true(self):
        assert QueryBuilder().sampled().build() == "traceSampled=true"

    def test_sampled_false(self):
        assert QueryBuilder().sampled(False).build() == "traceSampled=false"


class TestOperation:
    def test_operation_id(self):
        assert QueryBuilder().operation_id("op-123").build() == 'operation.id="op-123"'

    def test_operation_producer(self):
        assert (
            QueryBuilder().operation_producer("cloudsql.googleapis.com").build()
            == 'operation.producer:"cloudsql.googleapis.com"'
        )


class TestOtherFields:
    def test_insert_id(self):
        assert QueryBuilder().insert_id("abc123").build() == "insertId=abc123"

    def test_source_location_file(self):
        assert (
            QueryBuilder().source_location(file="main.py").build()
            == 'sourceLocation.file:"main.py"'
        )

    def test_source_location_function(self):
        assert (
            QueryBuilder().source_location(function="handle_request").build()
            == "sourceLocation.function:handle_request"
        )

    def test_source_location_both(self):
        q = (
            QueryBuilder()
            .source_location(file="main.py", function="handle_request")
            .build()
        )
        assert 'sourceLocation.file:"main.py"' in q
        assert "sourceLocation.function:handle_request" in q


class TestGlobalSearch:
    def test_simple_value(self):
        assert QueryBuilder().global_search("timeout").build() == '"timeout"'

    def test_value_with_colon(self):
        assert (
            QueryBuilder().global_search("ValueError: Bad").build()
            == '"ValueError: Bad"'
        )

    def test_value_with_space(self):
        assert QueryBuilder().global_search("disk full").build() == '"disk full"'

    def test_value_with_double_quote_escaped(self):
        assert QueryBuilder().global_search('say "hi"').build() == '"say \\"hi\\""'

    def test_combined_with_other_filters(self):
        q = (
            QueryBuilder()
            .severity_gte(Severity.ERROR)
            .global_search("ValueError: Bad")
            .build()
        )
        assert q == 'severity>=ERROR\n"ValueError: Bad"'


class TestWhereAndRaw:
    def test_where_with_f_expr(self):
        q = QueryBuilder().where(F("severity") >= "ERROR").build()
        assert q == "severity>=ERROR"

    def test_raw_passthrough(self):
        q = QueryBuilder().raw('jsonPayload.message=~".*panic.*"').build()
        assert q == 'jsonPayload.message=~".*panic.*"'

    def test_where_or_expression(self):
        q = (
            QueryBuilder()
            .where(
                (F("resource.type") == "cloud_run_revision")
                | (F("resource.type") == "cloud_function")
            )
            .build()
        )
        assert (
            q == "(resource.type=cloud_run_revision) OR (resource.type=cloud_function)"
        )


class TestMultipleFilters:
    def test_filters_joined_by_newline(self):
        q = (
            QueryBuilder()
            .resource_type(ResourceType.CLOUD_RUN_REVISION)
            .severity_gte(Severity.ERROR)
            .build()
        )
        lines = q.splitlines()
        assert len(lines) == 2
        assert lines[0] == "resource.type=cloud_run_revision"
        assert lines[1] == "severity>=ERROR"

    def test_readme_example(self):
        q = (
            QueryBuilder()
            .severity_gte(Severity.ERROR)
            .time_range(
                start=datetime(2026, 7, 9, 10, 0, 0),
                end=datetime(2026, 7, 9, 10, 30, 0),
            )
            .where(
                F("textPayload").has("ValueError: Bad")
                | F("jsonPayload.message").has("ValueError: Bad")
            )
            .build()
        )
        assert "severity>=ERROR" in q
        assert 'timestamp>="2026-07-09T10:00:00Z"' in q
        assert 'timestamp<"2026-07-09T10:30:00Z"' in q
        assert (
            '(textPayload:"ValueError: Bad") OR (jsonPayload.message:"ValueError: Bad")'
            in q
        )
