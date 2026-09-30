"""Local ATS fixture routing for safe application-flow development."""
from __future__ import annotations

from urllib.parse import urlsplit

TEST_ATS_SCENARIOS = ("basic", "multistep", "weird-ui", "dynamic")


def validate_base_url(base_url: str) -> str:
    """No arbitrary destination, credentials, DNS aliases, or path prefixes."""
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("Test ATS base URL must be a loopback HTTP(S) origin with an explicit port")
    return base_url.rstrip("/")


def fixture_url(job_id: int, base_url: str) -> str:
    """Return a stable fixture URL, spreading jobs across all test scenarios."""
    scenario = TEST_ATS_SCENARIOS[(job_id - 1) % len(TEST_ATS_SCENARIOS)]
    return f"{validate_base_url(base_url)}/test-ats/{scenario}"
