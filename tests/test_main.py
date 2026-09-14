from tests.test_api_sync import api  # noqa: F401  (fixture)


def test_a_public_path_is_cached_for_an_anonymous_visitor(api):
    client, _ = api
    resp = client.get("/stats")
    assert resp.headers["cache-control"] == "public, max-age=86400"


def test_a_public_path_is_never_cached_for_a_logged_in_session(api):
    client, _ = api
    client.cookies.set("authelia_session", "whatever")
    resp = client.get("/stats")
    assert resp.headers["cache-control"] == "private, no-store"


def test_a_path_off_the_allowlist_is_never_cached(api):
    client, _ = api
    resp = client.get("/health")
    assert resp.headers["cache-control"] == "private, no-store"
