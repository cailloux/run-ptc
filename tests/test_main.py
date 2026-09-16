from tests.test_api_sync import api  # noqa: F401  (fixture)

PUBLIC_HOST = "progress.runptc.com"
ADMIN_HOST = "admin.runptc.com"


def test_public_host_and_allowlisted_path_is_cached(api):
    client, _ = api
    resp = client.get("/tokens.css", headers={"Host": PUBLIC_HOST})
    assert resp.headers["cache-control"] == "public, max-age=86400"


def test_admin_host_is_never_cached_even_on_the_same_path(api):
    client, _ = api
    resp = client.get("/tokens.css", headers={"Host": ADMIN_HOST})
    assert resp.headers["cache-control"] == "private, no-store"


def test_public_host_but_a_non_allowlisted_path_is_never_cached(api):
    client, _ = api
    resp = client.get("/status", headers={"Host": PUBLIC_HOST})
    assert resp.headers["cache-control"] == "private, no-store"
