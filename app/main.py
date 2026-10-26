from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, EmailStr, Field

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

SEED_FILE = Path(__file__).resolve().parents[1] / "seed_data.json"
TOKEN_TTL_SECONDS = 60 * 60 * 12
ORDER_STATUSES = {"processing", "shipped", "delivered", "cancelled"}


def database_path() -> Path:
    configured_path = os.getenv("DATABASE_PATH", "./aurex.sqlite3")
    path = Path(configured_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path


def connect_database() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_database() -> None:
    with connect_database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deals (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consultations (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vip_subscribers (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL,
                token TEXT NOT NULL
            );
            """
        )

        if connection.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            return

        if not SEED_FILE.is_file():
            raise RuntimeError(f"Backend seed data is missing: {SEED_FILE}")

        seed_data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        connection.executemany(
            "INSERT INTO products (id, data) VALUES (?, ?)",
            [
                (
                    product["id"],
                    json.dumps(
                        {**product, "stock": product.get("stock", 10)},
                        separators=(",", ":"),
                    ),
                )
                for product in seed_data["products"]
            ],
        )
        connection.executemany(
            "INSERT INTO deals (id, data) VALUES (?, ?)",
            [
                (deal["id"], json.dumps(deal, separators=(",", ":")))
                for deal in seed_data["deals"]
            ],
        )
        connection.execute(
            "INSERT INTO settings (key, data) VALUES ('hero_deal', ?)",
            (json.dumps(seed_data["hero"], separators=(",", ":")),),
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="AUREX Haute Parfumerie API",
    version="1.0.0",
    description="Store, order, and administration API for the AUREX storefront.",
    lifespan=lifespan,
)

cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "x-admin-token", "Authorization"],
)


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ProductInput(FlexibleModel):
    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    price: float = Field(ge=0)
    stock: int = Field(default=10, ge=0)


class DealInput(FlexibleModel):
    id: str = Field(default="", max_length=120)
    name: str = Field(min_length=1, max_length=200)
    price: float = Field(ge=0)


class OrderItem(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    qty: int = Field(ge=1, le=100)
    price: float = Field(ge=0)


class OrderInput(BaseModel):
    customer: str = Field(min_length=1, max_length=200)
    email: str = Field(default="", max_length=254)
    location: str = Field(min_length=1, max_length=500)
    items: list[OrderItem] = Field(min_length=1, max_length=100)
    payment: str = Field(default="WhatsApp Order", max_length=100)
    shipping: str = Field(default="Express Delivery Included", max_length=200)


class OrderStatusInput(BaseModel):
    status: str


class ConsultationInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    salon: str = Field(min_length=1, max_length=200)
    scentPreference: str = Field(min_length=1, max_length=300)
    date: str = Field(default="", max_length=30)


class VIPInput(BaseModel):
    email: EmailStr


def _encode_token(payload: dict[str, Any], secret: str) -> str:
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(
        secret.encode("utf-8"), encoded_payload, hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"{encoded_payload.decode('ascii')}.{encoded_signature.decode('ascii')}"


def _valid_admin_token(token: str, secret: str) -> bool:
    try:
        encoded_payload, encoded_signature = token.split(".", maxsplit=1)
        supplied_signature = base64.urlsafe_b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4)
        )
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return False

        payload = json.loads(
            base64.urlsafe_b64decode(
                encoded_payload + "=" * (-len(encoded_payload) % 4)
            )
        )
        return payload.get("sub") == "admin" and payload.get("exp", 0) > time.time()
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        return False


def require_admin(
    token: Annotated[str | None, Header(alias="x-admin-token")] = None,
) -> None:
    secret = os.getenv("SECRET_KEY")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured.",
        )
    if not token or not _valid_admin_token(token, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid or missing admin credentials.",
        )


def _json_data(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["data"])


def _get_product(product_id: str) -> dict[str, Any] | None:
    with connect_database() as connection:
        row = connection.execute(
            "SELECT data FROM products WHERE id = ?", (product_id,)
        ).fetchone()
    return json.loads(row["data"]) if row else None


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "online",
        "brand": "AUREX Haute Parfumerie API",
        "version": app.version,
        "timestamp": datetime.now().astimezone().isoformat(),
    }


@app.get("/")
def api_root() -> dict[str, str]:
    return {
        "name": app.title,
        "status": "online",
        "docs": "/docs",
        "health": "/api/health",
    }


@app.post("/api/auth/login")
def login(credentials: LoginRequest) -> dict[str, Any]:
    admin_password = os.getenv("ADMIN_PASSWORD")
    secret = os.getenv("SECRET_KEY")
    if not admin_password or not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured.",
        )
    if not hmac.compare_digest(credentials.password, admin_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password. Access denied.",
        )

    token = _encode_token(
        {"sub": "admin", "exp": int(time.time()) + TOKEN_TTL_SECONDS}, secret
    )
    return {
        "success": True,
        "message": "Authentication successful",
        "token": token,
        "user": {"role": "admin", "name": "AUREX Concierge Admin"},
    }


@app.get("/api/auth/verify", dependencies=[Depends(require_admin)])
def verify_token() -> dict[str, bool]:
    return {"success": True, "valid": True}


@app.get("/api/products")
def list_products(
    search: str = Query(default="", max_length=200),
    category: str = Query(default="", max_length=120),
) -> dict[str, Any]:
    with connect_database() as connection:
        rows = connection.execute("SELECT data FROM products ORDER BY rowid").fetchall()
    products = [_json_data(row) for row in rows]
    if search:
        search_lower = search.casefold()
        products = [
            product
            for product in products
            if search_lower in product.get("name", "").casefold()
            or search_lower in product.get("type", "").casefold()
        ]
    if category:
        products = [
            product for product in products if product.get("category") == category
        ]
    return {"success": True, "data": products}


@app.get("/api/products/{product_id}")
def get_product(product_id: str) -> dict[str, Any]:
    product = _get_product(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found.")
    return {"success": True, "data": product}


@app.post("/api/products", dependencies=[Depends(require_admin)])
def create_product(product: ProductInput) -> dict[str, Any]:
    product_data = product.model_dump()
    try:
        with connect_database() as connection:
            connection.execute(
                "INSERT INTO products (id, data) VALUES (?, ?)",
                (product.id, json.dumps(product_data, separators=(",", ":"))),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="Product already exists.") from error
    return {"success": True, "data": product_data}


@app.put("/api/products/{product_id}", dependencies=[Depends(require_admin)])
def update_product(product_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    product = _get_product(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found.")
    updated = {**product, **updates, "id": product_id}
    validated = ProductInput.model_validate(updated).model_dump()
    with connect_database() as connection:
        connection.execute(
            "UPDATE products SET data = ? WHERE id = ?",
            (json.dumps(validated, separators=(",", ":")), product_id),
        )
    return {"success": True, "data": validated}


@app.delete("/api/products/{product_id}", dependencies=[Depends(require_admin)])
def delete_product(product_id: str) -> dict[str, Any]:
    with connect_database() as connection:
        cursor = connection.execute("DELETE FROM products WHERE id = ?", (product_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Product not found.")
    return {"success": True, "message": "Product deleted."}


@app.get("/api/deals")
def list_deals() -> dict[str, Any]:
    with connect_database() as connection:
        rows = connection.execute("SELECT data FROM deals ORDER BY rowid").fetchall()
        hero_row = connection.execute(
            "SELECT data FROM settings WHERE key = 'hero_deal'"
        ).fetchone()
    return {
        "success": True,
        "data": {
            "hero": json.loads(hero_row["data"]) if hero_row else None,
            "curated": [_json_data(row) for row in rows],
        },
    }


@app.put("/api/deals/hero", dependencies=[Depends(require_admin)])
def update_hero_deal(deal: dict[str, Any]) -> dict[str, Any]:
    with connect_database() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, data) VALUES ('hero_deal', ?)
            ON CONFLICT(key) DO UPDATE SET data = excluded.data
            """,
            (json.dumps(deal, separators=(",", ":")),),
        )
    return {"success": True, "data": deal}


@app.post("/api/deals", dependencies=[Depends(require_admin)])
def create_deal(deal: DealInput) -> dict[str, Any]:
    deal_data = deal.model_dump()
    if not deal_data["id"]:
        deal_data["id"] = re.sub(
            r"[^a-z0-9-]+", "-", deal_data["name"].casefold()
        ).strip("-") or f"deal-{secrets.token_hex(4)}"
    try:
        with connect_database() as connection:
            connection.execute(
                "INSERT INTO deals (id, data) VALUES (?, ?)",
                (deal.id, json.dumps(deal_data, separators=(",", ":"))),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="Deal already exists.") from error
    return {"success": True, "data": deal_data}


@app.put("/api/deals/{deal_id}", dependencies=[Depends(require_admin)])
def update_deal(deal_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    with connect_database() as connection:
        row = connection.execute(
            "SELECT data FROM deals WHERE id = ?", (deal_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Deal not found.")
        updated = {**json.loads(row["data"]), **updates, "id": deal_id}
        validated = DealInput.model_validate(updated).model_dump()
        connection.execute(
            "UPDATE deals SET data = ? WHERE id = ?",
            (json.dumps(validated, separators=(",", ":")), deal_id),
        )
    return {"success": True, "data": validated}


@app.delete("/api/deals/{deal_id}", dependencies=[Depends(require_admin)])
def delete_deal(deal_id: str) -> dict[str, Any]:
    with connect_database() as connection:
        cursor = connection.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Deal not found.")
    return {"success": True, "message": "Deal deleted."}


@app.post("/api/orders", status_code=status.HTTP_201_CREATED)
def create_order(order: OrderInput) -> dict[str, Any]:
    order_id = f"AUR-{secrets.token_hex(4).upper()}"
    order_data = {
        **order.model_dump(),
        "id": order_id,
        "total": round(sum(item.qty * item.price for item in order.items), 2),
        "status": "processing",
        "date": date.today().isoformat(),
    }
    created_at = datetime.now().astimezone().isoformat()
    with connect_database() as connection:
        connection.execute(
            "INSERT INTO orders (id, status, created_at, data) VALUES (?, ?, ?, ?)",
            (
                order_id,
                order_data["status"],
                created_at,
                json.dumps(order_data, separators=(",", ":")),
            ),
        )
    return {"success": True, "data": order_data}


@app.get("/api/orders", dependencies=[Depends(require_admin)])
def list_orders(
    order_status: str = Query(default="", alias="status", max_length=30),
) -> dict[str, Any]:
    with connect_database() as connection:
        if order_status:
            rows = connection.execute(
                "SELECT data FROM orders WHERE status = ? ORDER BY created_at DESC",
                (order_status,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT data FROM orders ORDER BY created_at DESC"
            ).fetchall()
    return {"success": True, "data": [_json_data(row) for row in rows]}


@app.patch("/api/orders/{order_id}/status", dependencies=[Depends(require_admin)])
def update_order_status(order_id: str, update: OrderStatusInput) -> dict[str, Any]:
    if update.status not in ORDER_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid order status.")
    with connect_database() as connection:
        row = connection.execute(
            "SELECT data FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Order not found.")
        order = json.loads(row["data"])
        order["status"] = update.status
        connection.execute(
            "UPDATE orders SET status = ?, data = ? WHERE id = ?",
            (update.status, json.dumps(order, separators=(",", ":")), order_id),
        )
    return {"success": True, "data": order}


@app.post("/api/consultations", status_code=status.HTTP_201_CREATED)
def create_consultation(consultation: ConsultationInput) -> dict[str, Any]:
    consultation_id = f"CON-{secrets.token_hex(4).upper()}"
    created_at = datetime.now().astimezone().isoformat()
    data = {
        **consultation.model_dump(),
        "id": consultation_id,
        "status": "pending",
        "date": consultation.date or date.today().isoformat(),
    }
    with connect_database() as connection:
        connection.execute(
            "INSERT INTO consultations (id, created_at, data) VALUES (?, ?, ?)",
            (consultation_id, created_at, json.dumps(data, separators=(",", ":"))),
        )
    return {"success": True, "data": data}


@app.get("/api/consultations", dependencies=[Depends(require_admin)])
def list_consultations() -> dict[str, Any]:
    with connect_database() as connection:
        rows = connection.execute(
            "SELECT data FROM consultations ORDER BY created_at DESC"
        ).fetchall()
    return {"success": True, "data": [_json_data(row) for row in rows]}


@app.post("/api/vip", status_code=status.HTTP_201_CREATED)
def subscribe_vip(subscription: VIPInput) -> dict[str, Any]:
    email = str(subscription.email)
    token = f"AUR-{secrets.token_hex(3).upper()}-VIP"
    subscriber_id = secrets.token_hex(8)
    created_at = date.today().isoformat()
    with connect_database() as connection:
        existing = connection.execute(
            "SELECT token FROM vip_subscribers WHERE email = ? COLLATE NOCASE",
            (email,),
        ).fetchone()
        if existing:
            return {"success": True, "token": existing["token"]}
        connection.execute(
            "INSERT INTO vip_subscribers (id, email, created_at, token) VALUES (?, ?, ?, ?)",
            (subscriber_id, email, created_at, token),
        )
    return {"success": True, "token": token}


@app.get("/api/dashboard/stats", dependencies=[Depends(require_admin)])
def dashboard_stats() -> dict[str, Any]:
    with connect_database() as connection:
        products_count = connection.execute(
            "SELECT COUNT(*) AS count FROM products"
        ).fetchone()["count"]
        orders = [
            json.loads(row["data"])
            for row in connection.execute(
                "SELECT data FROM orders ORDER BY created_at DESC"
            ).fetchall()
        ]

    active_orders = [order for order in orders if order["status"] != "cancelled"]
    revenue = round(sum(order["total"] for order in active_orders), 2)
    total_orders = len(orders)
    average_order_value = round(revenue / len(active_orders), 2) if active_orders else 0
    months: list[tuple[int, int]] = []
    today = date.today()
    year, month = today.year, today.month
    for _ in range(6):
        months.append((year, month))
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    months.reverse()
    monthly_revenue = []
    for year, month in months:
        month_orders = [
            order
            for order in orders
            if datetime.fromisoformat(order["date"]).year == year
            and datetime.fromisoformat(order["date"]).month == month
        ]
        monthly_revenue.append(
            {
                "month": date(year, month, 1).strftime("%b"),
                "revenue": round(
                    sum(
                        order["total"]
                        for order in month_orders
                        if order["status"] != "cancelled"
                    ),
                    2,
                ),
                "orders": len(month_orders),
            }
        )
    order_counts = {
        order_status: sum(order["status"] == order_status for order in orders)
        for order_status in ORDER_STATUSES
    }
    return {
        "success": True,
        "data": {
            "stats": {
                "totalRevenue": revenue,
                "revenueGrowth": 0,
                "totalOrders": total_orders,
                "ordersGrowth": 0,
                "totalProducts": products_count,
                "avgOrderValue": average_order_value,
                "avgOrderGrowth": 0,
                "pendingOrders": order_counts["processing"],
                "shippedOrders": order_counts["shipped"],
                "deliveredOrders": order_counts["delivered"],
            },
            "monthlyRevenue": monthly_revenue,
            "recentOrders": orders[:5],
        },
    }
