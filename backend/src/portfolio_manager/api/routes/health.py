"""Health and readiness routes.

``/health`` reports process liveness; ``/ready`` reports operational readiness
(database reachable). Both are unauthenticated and expose no sensitive data
(ADR-007), letting the Rust supervisor distinguish liveness from readiness.

``/health`` also supports an identity challenge. The supervisor picks the port
before the sidecar binds it, so another local process could win that race. When
a request carries ``X-Challenge: <nonce>`` and a token is configured, the reply
includes ``proof`` = hex ``HMAC-SHA256(token, nonce)``. Only a process that knows
the per-launch token can compute it, so the supervisor can confirm it is talking
to the real sidecar before sending the token anywhere.
"""

import hashlib
import hmac

from fastapi import APIRouter, Depends, Request

from portfolio_manager.api.dependencies import get_container
from portfolio_manager.bootstrap import Container

router = APIRouter(tags=["health"])

#: Request header carrying the supervisor's nonce.
CHALLENGE_HEADER = "X-Challenge"

#: Longest nonce accepted; the supervisor sends 32 hex characters.
MAX_CHALLENGE_LENGTH = 128


def compute_proof(token: str, nonce: str) -> str:
    """Return the identity proof for *nonce*: hex ``HMAC-SHA256(token, nonce)``.

    :param token: The per-launch sidecar token.
    :param nonce: The supervisor's challenge.
    :rtype: str
    """
    return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()


@router.get("/health")
async def health(request: Request, container: Container = Depends(get_container)) -> dict[str, str]:
    """Report process liveness, with an identity proof when challenged.

    :returns: ``{"status": "ok"}``, plus ``proof`` when a valid challenge is sent
        and a token is configured.
    :rtype: dict[str, str]
    """
    body = {"status": "ok"}
    nonce = request.headers.get(CHALLENGE_HEADER)
    if nonce and container.token and len(nonce) <= MAX_CHALLENGE_LENGTH:
        body["proof"] = compute_proof(container.token, nonce)
    return body


@router.get("/ready")
async def ready(container: Container = Depends(get_container)) -> dict[str, str]:
    """Report operational readiness by probing the database.

    :returns: ``{"status": "ready"}`` when the database responds.
    :rtype: dict[str, str]
    """
    container.db.fetchone("SELECT 1")
    return {"status": "ready"}
