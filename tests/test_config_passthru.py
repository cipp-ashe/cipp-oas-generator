"""Tests for passthru-related config additions."""
import re


def test_passthru_endpoints_is_set_of_strings():
    from config import PASSTHRU_ENDPOINTS
    assert isinstance(PASSTHRU_ENDPOINTS, set)
    assert all(isinstance(e, str) for e in PASSTHRU_ENDPOINTS)
    assert "ListGraphRequest" in PASSTHRU_ENDPOINTS


def test_graph_url_normalize_re_is_valid_regex():
    from config import GRAPH_URL_NORMALIZE_RE
    assert isinstance(GRAPH_URL_NORMALIZE_RE, str)
    compiled = re.compile(GRAPH_URL_NORMALIZE_RE)
    m = compiled.match("https://graph.microsoft.com/v1.0/users")
    assert m is not None
    assert m.group(1) == "/users"


def test_graph_url_normalize_re_matches_beta():
    from config import GRAPH_URL_NORMALIZE_RE
    compiled = re.compile(GRAPH_URL_NORMALIZE_RE)
    m = compiled.match("https://graph.microsoft.com/beta/groups")
    assert m is not None
    assert m.group(1) == "/groups"


def test_passthru_coverage_tier_exists():
    from config import COVERAGE_TIERS
    assert "PASSTHRU" in COVERAGE_TIERS
