"""Contract tests for token authentication and health routes."""


def test_health_is_unauthenticated(client) -> None:
    assert client.get("/health").status_code == 200


def test_health_without_challenge_has_no_proof(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_health_challenge_returns_hmac_proof(client, test_token: str) -> None:
    import hashlib
    import hmac

    nonce = "00112233445566778899aabbccddeeff"
    body = client.get("/health", headers={"X-Challenge": nonce}).json()
    expected = hmac.new(test_token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    assert body == {"status": "ok", "proof": expected}


def test_health_proof_matches_rust_reference_vector() -> None:
    """Same vector as ``security::identity`` tests in the Rust shell."""
    from portfolio_manager.api.routes.health import compute_proof

    assert compute_proof("test-token-123", "00112233445566778899aabbccddeeff") == (
        "b72369d397d74cff8ca0f73b6e02313b00c8463c247fca73270aead986734781"
    )


def test_health_oversized_challenge_is_ignored(client) -> None:
    body = client.get("/health", headers={"X-Challenge": "a" * 129}).json()
    assert "proof" not in body


def test_health_challenge_without_token_gives_no_proof(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from portfolio_manager.api.app import create_app
    from portfolio_manager.bootstrap import build_container

    container = build_container(
        config_path=tmp_path / "config.toml", db_path=":memory:", token="", production=True
    )
    body = TestClient(create_app(container)).get("/health", headers={"X-Challenge": "abc"}).json()
    assert "proof" not in body


def test_ready_is_unauthenticated(client) -> None:
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_business_route_requires_token(client) -> None:
    resp = client.get("/api/v1/projects")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "AUTHENTICATION_FAILED"
    assert "correlation_id" in body


def test_business_route_rejects_wrong_token(client) -> None:
    resp = client.get("/api/v1/projects", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


def test_business_route_accepts_valid_token(client, test_token: str) -> None:
    resp = client.get("/api/v1/projects", headers={"X-API-Key": test_token})
    assert resp.status_code == 200


def test_correlation_id_header_present(auth_client) -> None:
    resp = auth_client.get("/api/v1/projects")
    assert resp.headers.get("X-Correlation-ID")
