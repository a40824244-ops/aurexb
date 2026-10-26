import secrets
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_store_api_and_admin_access(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("SECRET_KEY", secrets.token_hex(32))

    with TestClient(app) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert root.json()["docs"] == "/docs"
        assert root.json()["health"] == "/api/health"

        products = client.get("/api/products")
        assert products.status_code == 200
        assert products.json()["success"] is True
        assert len(products.json()["data"]) >= 1

        assert client.get("/api/orders").status_code == 401
        login = client.post("/api/auth/login", json={"password": "wrong"})
        assert login.status_code == 401

        login = client.post("/api/auth/login", json={"password": "test-password"})
        assert login.status_code == 200
        token = login.json()["token"]
        headers = {"x-admin-token": token}
        assert client.get("/api/auth/verify", headers=headers).json()["valid"] is True
        assert client.get("/api/orders", headers=headers).json()["data"] == []

        product = client.post(
            "/api/products",
            headers=headers,
            json={"id": "test-product", "name": "Test Perfume", "price": 125},
        )
        assert product.status_code == 200
        assert product.json()["data"]["stock"] == 10
        changed_product = client.put(
            "/api/products/test-product",
            headers=headers,
            json={"stock": 5},
        )
        assert changed_product.json()["data"]["stock"] == 5

        deal = client.post(
            "/api/deals",
            headers=headers,
            json={"id": "", "name": "Test Deal", "price": 250},
        )
        assert deal.status_code == 200
        deal_id = deal.json()["data"]["id"]
        assert deal_id == "test-deal"
        assert client.get("/api/deals").json()["data"]["curated"][-1]["id"] == deal_id

        created_order = client.post(
            "/api/orders",
            json={
                "customer": "Test Customer",
                "email": "",
                "location": "WhatsApp Order",
                "items": [{"name": "AUREX Test", "qty": 2, "price": 100}],
            },
        )
        assert created_order.status_code == 201
        order = created_order.json()["data"]
        assert order["total"] == 200
        assert order["status"] == "processing"

        updated = client.patch(
            f"/api/orders/{order['id']}/status",
            headers=headers,
            json={"status": "shipped"},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["status"] == "shipped"
        stats = client.get("/api/dashboard/stats", headers=headers).json()["data"]
        assert stats["stats"]["totalOrders"] == 1
        assert stats["stats"]["shippedOrders"] == 1
        assert client.get("/api/auth/verify", headers={"x-admin-token": "."}).status_code == 401

        consultation = client.post(
            "/api/consultations",
            json={
                "name": "Test Customer",
                "email": "customer@example.com",
                "salon": "Dubai Lounge",
                "scentPreference": "Oudh",
            },
        )
        assert consultation.status_code == 201
        assert client.get("/api/consultations", headers=headers).json()["data"]

        first_vip = client.post(
            "/api/vip", json={"email": "vip@example.com"}
        ).json()["token"]
        second_vip = client.post(
            "/api/vip", json={"email": "VIP@example.com"}
        ).json()["token"]
        assert first_vip == second_vip


def test_admin_authentication_fails_closed_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "unconfigured.sqlite3"))
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"password": "anything"})
        assert login.status_code == 503
        assert client.get("/api/orders").status_code == 503
