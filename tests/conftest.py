"""Testumgebung: Betriebsmodus mit festem Testschlüssel statt echtem Portal."""

import os
import time
from types import SimpleNamespace

os.environ.pop("PORTAL_DEV", None)
os.environ["PORTAL_ENV"] = "test"
os.environ["PORTAL_ISSUER"] = "https://portal.test/"

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.main import app, portal
from platform_sdk import layout as layout_module
from platform_sdk.adapters import transport as transport_module
from platform_sdk.adapters.bwsync.testing import FakeBwSync
from platform_sdk.jwks import JwksCache

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
portal.jwks = JwksCache.static({"test-key": PRIVATE_KEY.public_key()})


def make_token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://portal.test/",
        "aud": [portal.config.slug, "portal", "adapters"],
        "sub": "test-person",
        "name": "Erika Musterfrau",
        "roles": ["student"],
        "v": 1,
        "iat": now,
        "exp": now + 60,
    }
    claims.update(overrides)
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"})


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def as_student():
    return {"X-Portal-Assertion": make_token(), "Accept": "text/html"}


@pytest.fixture(autouse=True)
def adapter_http(monkeypatch):
    """Kein echtes Netz: HTTP-Aufrufe des SDK landen hier (nur für Tests, die den Fake ausbauen)."""
    requests: list[httpx.Request] = []

    def remote(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"path": "", "entries": [], "truncated": False, "skipped": 0})

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(remote), **kwargs)

    def nav_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs)

    monkeypatch.setattr(
        transport_module,
        "httpx",
        SimpleNamespace(
            AsyncClient=factory,
            TimeoutException=httpx.TimeoutException,
            Timeout=httpx.Timeout,
            HTTPError=httpx.HTTPError,
        ),
    )
    monkeypatch.setattr(layout_module, "httpx", SimpleNamespace(AsyncClient=nav_factory, HTTPError=httpx.HTTPError))
    return requests


@pytest.fixture(autouse=True)
def adapter_backend(adapter_http):
    """bwSync-Konto im Speicher (Fake aus dem SDK, gleiche Fehlertypen wie der Adapter)."""
    backend = FakeBwSync().install(portal)
    yield backend
    backend.uninstall()
