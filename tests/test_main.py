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


def test_public_host_and_a_non_allowlisted_path_404s(api):
    client, _ = api
    resp = client.get("/status", headers={"Host": PUBLIC_HOST})
    assert resp.status_code == 404


def test_admin_host_reaches_the_same_path_fine(api):
    client, _ = api
    resp = client.get("/status", headers={"Host": ADMIN_HOST})
    assert resp.status_code == 200


def test_public_host_and_an_allowlisted_path_is_not_gated(api):
    client, _ = api
    resp = client.get("/progress", headers={"Host": PUBLIC_HOST})
    assert resp.status_code == 200


def test_public_host_blocks_a_mutating_admin_endpoint_too(api):
    client, _ = api
    resp = client.post("/sync", headers={"Host": PUBLIC_HOST})
    assert resp.status_code == 404
