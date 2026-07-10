from gcp_observability.logging.expressions import (
    And,
    Comparison,
    F,
    Not,
    Or,
    Raw,
    _format_value,
)


class TestFormatValue:
    def test_bool_true(self):
        assert _format_value(True) == "true"

    def test_bool_false(self):
        assert _format_value(False) == "false"

    def test_int(self):
        assert _format_value(500) == "500"

    def test_float(self):
        assert _format_value(2.5) == "2.5"

    def test_simple_string_no_quotes(self):
        assert _format_value("ERROR") == "ERROR"
        assert _format_value("cloud_run_revision") == "cloud_run_revision"

    def test_string_with_slash_gets_quoted(self):
        assert (
            _format_value("projects/my-project/logs/app")
            == '"projects/my-project/logs/app"'
        )

    def test_string_with_space_gets_quoted(self):
        assert _format_value("hello world") == '"hello world"'

    def test_string_with_colon_gets_quoted(self):
        assert _format_value("2024-06-01T00:00:00Z") == '"2024-06-01T00:00:00Z"'

    def test_string_with_double_quote_escaped(self):
        assert _format_value('say "hi"') == '"say \\"hi\\""'

    def test_string_with_backslash_escaped(self):
        assert _format_value("a\\b") == '"a\\\\b"'


class TestComparison:
    def test_eq(self):
        assert Comparison("severity", "=", "ERROR").build() == "severity=ERROR"

    def test_gte(self):
        assert Comparison("severity", ">=", "WARNING").build() == "severity>=WARNING"

    def test_lt(self):
        assert (
            Comparison("httpRequest.status", "<", 500).build()
            == "httpRequest.status<500"
        )

    def test_has(self):
        assert (
            Comparison("textPayload", ":", "timeout").build() == "textPayload:timeout"
        )

    def test_quoted_value(self):
        assert (
            Comparison("logName", "=", "projects/p/logs/app").build()
            == 'logName="projects/p/logs/app"'
        )


class TestAnd:
    def test_two_exprs(self):
        expr = And(
            Comparison("severity", ">=", "ERROR"),
            Comparison("resource.type", "=", "gce_instance"),
        )
        assert expr.build() == "severity>=ERROR\nAND resource.type=gce_instance"

    def test_flattens_nested_and(self):
        a = Comparison("a", "=", "1")
        b = Comparison("b", "=", "2")
        c = Comparison("c", "=", "3")
        expr = And(And(a, b), c)
        assert expr.build() == "a=1\nAND b=2\nAND c=3"

    def test_parenthesises_or_child(self):
        or_expr = Or(Comparison("a", "=", "1"), Comparison("b", "=", "2"))
        and_expr = And(Comparison("c", "=", "3"), or_expr)
        assert and_expr.build() == "c=3\nAND ((a=1) OR (b=2))"

    def test_operator_shorthand(self):
        a = Comparison("a", "=", "1")
        b = Comparison("b", "=", "2")
        result = (a & b).build()
        assert result == "a=1\nAND b=2"

    def test_chained_operator_flattens(self):
        a = Comparison("a", "=", "1")
        b = Comparison("b", "=", "2")
        c = Comparison("c", "=", "3")
        result = (a & b & c).build()
        assert result == "a=1\nAND b=2\nAND c=3"


class TestOr:
    def test_two_exprs(self):
        expr = Or(
            Comparison("resource.type", "=", "cloud_run_revision"),
            Comparison("resource.type", "=", "cloud_function"),
        )
        assert (
            expr.build()
            == "(resource.type=cloud_run_revision) OR (resource.type=cloud_function)"
        )

    def test_flattens_nested_or(self):
        a = Comparison("a", "=", "1")
        b = Comparison("b", "=", "2")
        c = Comparison("c", "=", "3")
        expr = Or(Or(a, b), c)
        assert expr.build() == "(a=1) OR (b=2) OR (c=3)"

    def test_operator_shorthand(self):
        a = Comparison("a", "=", "1")
        b = Comparison("b", "=", "2")
        result = (a | b).build()
        assert result == "(a=1) OR (b=2)"

    def test_chained_operator_flattens(self):
        a = Comparison("a", "=", "1")
        b = Comparison("b", "=", "2")
        c = Comparison("c", "=", "3")
        result = (a | b | c).build()
        assert result == "(a=1) OR (b=2) OR (c=3)"


class TestNot:
    def test_simple(self):
        expr = Not(Comparison("textPayload", ":", "healthcheck"))
        assert expr.build() == "NOT textPayload:healthcheck"

    def test_wraps_and_in_parens(self):
        inner = And(Comparison("a", "=", "1"), Comparison("b", "=", "2"))
        assert Not(inner).build() == "NOT (a=1\nAND b=2)"

    def test_wraps_or_in_parens(self):
        inner = Or(Comparison("a", "=", "1"), Comparison("b", "=", "2"))
        assert Not(inner).build() == "NOT ((a=1) OR (b=2))"

    def test_invert_operator(self):
        expr = Comparison("textPayload", ":", "healthcheck")
        assert (~expr).build() == "NOT textPayload:healthcheck"


class TestRaw:
    def test_passthrough(self):
        assert (
            Raw('jsonPayload.message=~".*panic.*"').build()
            == 'jsonPayload.message=~".*panic.*"'
        )


class TestField:
    def test_eq(self):
        assert (F("severity") == "ERROR").build() == "severity=ERROR"

    def test_ne(self):
        assert (F("severity") != "DEBUG").build() == "severity!=DEBUG"

    def test_gt(self):
        assert (F("httpRequest.status") > 400).build() == "httpRequest.status>400"

    def test_lt(self):
        assert (F("httpRequest.status") < 500).build() == "httpRequest.status<500"

    def test_ge(self):
        assert (F("severity") >= "ERROR").build() == "severity>=ERROR"

    def test_le(self):
        assert (F("severity") <= "WARNING").build() == "severity<=WARNING"

    def test_has(self):
        assert F("textPayload").has("timeout").build() == "textPayload:timeout"

    def test_dot_chaining(self):
        assert (
            F("resource").labels.zone == "us-central1-a"
        ).build() == 'resource.labels.zone="us-central1-a"'

    def test_bracket_access_quotes_key(self):
        assert (
            F("labels")["k8s-pod/app"] == "my-service"
        ).build() == 'labels."k8s-pod/app"="my-service"'

    def test_bracket_access_simple_key(self):
        assert (
            F("resource.labels")["zone"] == "us-east1"
        ).build() == 'resource.labels."zone"="us-east1"'

    def test_and_operator(self):
        expr = (F("severity") >= "ERROR") & (F("resource.type") == "gce_instance")
        assert expr.build() == "severity>=ERROR\nAND resource.type=gce_instance"

    def test_or_operator(self):
        expr = (F("resource.type") == "cloud_run_revision") | (
            F("resource.type") == "cloud_function"
        )
        assert (
            "(resource.type=cloud_run_revision) OR (resource.type=cloud_function)"
            == expr.build()
        )

    def test_not_operator(self):
        expr = ~F("textPayload").has("healthcheck")
        assert expr.build() == "NOT textPayload:healthcheck"

    def test_complex_compound(self):
        expr = (
            (F("resource.type") == "gce_instance")
            & ((F("severity") >= "ERROR") | (F("jsonPayload.level") == "fatal"))
            & ~F("textPayload").has("healthcheck")
        )
        result = expr.build()
        assert "resource.type=gce_instance" in result
        assert "(severity>=ERROR) OR (jsonPayload.level=fatal)" in result
        assert "NOT textPayload:healthcheck" in result

    def test_str_returns_build(self):
        f = F("severity") >= "ERROR"
        assert str(f) == f.build()
