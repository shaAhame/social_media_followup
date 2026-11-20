from sqlalchemy import func, select

from app.config import get_settings
from app.models import Inquiry

HEADERS = {"X-API-Key": "test-api-key"}
BODY = {"phone": "0771234567", "source": "Instagram", "product": "iPhone 16", "timeframe": "today"}


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_api_requires_key(client):
    assert client.post("/api/v1/inquiries", json=BODY).status_code == 401
    assert (
        client.post("/api/v1/inquiries", json=BODY, headers={"X-API-Key": "x"}).status_code == 401
    )


def test_api_disabled_without_key(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_key", None)
    assert client.post("/api/v1/inquiries", json=BODY, headers=HEADERS).status_code == 404


def test_api_creates_inquiry(client, db):
    response = client.post("/api/v1/inquiries", json=BODY, headers=HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["created"] is True and data["status"] == "new"
    assert data["next_followup_at"]
    inquiry = db.get(Inquiry, data["id"])
    assert inquiry.channel.name == "Instagram"
    assert inquiry.created_by_id is None


def test_api_is_idempotent_with_external_ref(client, db):
    body = {**BODY, "external_ref": "wamid.HBgM123"}
    first = client.post("/api/v1/inquiries", json=body, headers=HEADERS).json()
    second = client.post("/api/v1/inquiries", json=body, headers=HEADERS).json()
    assert first["id"] == second["id"]
    assert second["created"] is False
    assert db.scalar(select(func.count()).select_from(Inquiry)) == 1


def test_api_validation(client):
    bad_source = client.post("/api/v1/inquiries", json={**BODY, "source": "Fax"}, headers=HEADERS)
    assert bad_source.status_code == 422
    bad_phone = client.post("/api/v1/inquiries", json={**BODY, "phone": "123"}, headers=HEADERS)
    assert bad_phone.status_code == 422
    assert "phone" in bad_phone.json()["detail"]
