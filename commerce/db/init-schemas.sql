-- ============================================================
-- DB 레벨 부트스트랩 DDL (docker-entrypoint-initdb.d 01번 / 각 서비스 schema.sql의 합본)
-- ============================================================
-- 각 서비스는 자기 스키마의 테이블을 스스로도 만든다(Spring Boot 기동 시
-- spring.sql.init.schema-locations: classpath:schema.sql, CREATE TABLE IF NOT EXISTS라 멱등).
-- 이 파일은 그와 별개로 "앱이 뜨기 전, DB 레벨에서" 스키마+테이블을 미리 갖춰두기 위한 것이다 —
-- 특히 02-seed-all.sql의 대량 시드가 어떤 서비스도 기동되지 않은 순수 postgres 컨테이너 위에서
-- 단독으로 실행 가능해야 하기 때문에(테스트/CI 검증, k8s 최초 부트스트랩) 필요하다.
--
-- (4번 증분에서 발견) 이전 버전은 CREATE SCHEMA만 있고 테이블 DDL이 없어서, 이 경로로
-- db/seed-all.sql을 단독 실행하면 "relation does not exist"로 전부 실패했다(각 서비스가
-- 기동해 자기 테이블을 먼저 만들어줘야만 우연히 성공하는 상태였다). 정합성을 위해 각 서비스
-- schema.sql의 CREATE TABLE을 그대로 옮겨왔다 — 두 곳에 DDL이 존재하는 중복은 감수하되,
-- 둘 다 CREATE TABLE IF NOT EXISTS라 어느 쪽이 먼저 실행되어도 충돌 없다.

CREATE SCHEMA IF NOT EXISTS order_schema;
CREATE SCHEMA IF NOT EXISTS product_schema;
CREATE SCHEMA IF NOT EXISTS inventory_schema;
CREATE SCHEMA IF NOT EXISTS payment_schema;
CREATE SCHEMA IF NOT EXISTS user_schema;
CREATE SCHEMA IF NOT EXISTS cart_schema;
CREATE SCHEMA IF NOT EXISTS pricing_schema;
CREATE SCHEMA IF NOT EXISTS shipping_schema;

-- ------------------------------------------------------------
-- product_schema (product-service)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_schema.categories (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    description TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS product_schema.products (
    id          BIGSERIAL PRIMARY KEY,
    category_id BIGINT REFERENCES product_schema.categories(id),
    name        VARCHAR(200) NOT NULL,
    description TEXT,
    price       DECIMAL(12,2) NOT NULL,
    image_url   VARCHAR(500),
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_products_category ON product_schema.products(category_id);
CREATE INDEX IF NOT EXISTS idx_products_name ON product_schema.products(name);

CREATE TABLE IF NOT EXISTS product_schema.product_variants (
    id            BIGSERIAL PRIMARY KEY,
    product_id    BIGINT NOT NULL REFERENCES product_schema.products(id),
    sku           VARCHAR(50) NOT NULL UNIQUE,
    variant_name  VARCHAR(100) NOT NULL,
    price_delta   DECIMAL(12,2) NOT NULL DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_product_variants_product ON product_schema.product_variants(product_id);

-- ------------------------------------------------------------
-- inventory_schema (inventory-service)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory_schema.inventory (
    id          BIGSERIAL PRIMARY KEY,
    product_id  BIGINT NOT NULL UNIQUE,
    stock       INT NOT NULL DEFAULT 0,
    reserved    INT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inventory_schema.inventory_movements (
    id               BIGSERIAL PRIMARY KEY,
    product_id       BIGINT NOT NULL,
    movement_type    VARCHAR(20) NOT NULL,
    quantity         INT NOT NULL,
    resulting_stock  INT NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_inventory_movements_product ON inventory_schema.inventory_movements(product_id, created_at DESC);

CREATE TABLE IF NOT EXISTS inventory_schema.outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_inventory_outbox_unpublished ON inventory_schema.outbox_events(created_at) WHERE published_at IS NULL;

-- ------------------------------------------------------------
-- user_schema (user-service)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_schema.users (
    id              BIGSERIAL PRIMARY KEY,
    email           VARCHAR(200) NOT NULL UNIQUE,
    password_hash   VARCHAR(100) NOT NULL,
    name            VARCHAR(100) NOT NULL,
    phone           VARCHAR(30),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_schema.addresses (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES user_schema.users(id),
    recipient_name  VARCHAR(100) NOT NULL,
    phone           VARCHAR(30),
    zipcode         VARCHAR(10),
    address1        VARCHAR(300) NOT NULL,
    address2        VARCHAR(200),
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_addresses_user ON user_schema.addresses(user_id);

CREATE TABLE IF NOT EXISTS user_schema.auth_tokens (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES user_schema.users(id),
    token           VARCHAR(64) NOT NULL UNIQUE,
    expires_at      TIMESTAMP NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_token ON user_schema.auth_tokens(token);

CREATE TABLE IF NOT EXISTS user_schema.outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_user_outbox_unpublished ON user_schema.outbox_events(created_at) WHERE published_at IS NULL;

-- ------------------------------------------------------------
-- cart_schema (cart-service)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cart_schema.carts (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL UNIQUE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cart_schema.cart_items (
    id          BIGSERIAL PRIMARY KEY,
    cart_id     BIGINT NOT NULL REFERENCES cart_schema.carts(id),
    product_id  BIGINT NOT NULL,
    quantity    INT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (cart_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_cart_items_cart ON cart_schema.cart_items(cart_id);

-- ------------------------------------------------------------
-- pricing_schema (pricing-service)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_schema.prices (
    id          BIGSERIAL PRIMARY KEY,
    product_id  BIGINT NOT NULL UNIQUE,
    base_price  DECIMAL(12,2) NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_prices_product ON pricing_schema.prices(product_id);

CREATE TABLE IF NOT EXISTS pricing_schema.promotions (
    id                  BIGSERIAL PRIMARY KEY,
    name                VARCHAR(100) NOT NULL,
    description         TEXT,
    discount_percent    DECIMAL(5,2) NOT NULL,
    starts_at           TIMESTAMP NOT NULL,
    ends_at             TIMESTAMP NOT NULL,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pricing_schema.coupons (
    id                  BIGSERIAL PRIMARY KEY,
    code                VARCHAR(50) NOT NULL UNIQUE,
    discount_amount     DECIMAL(12,2) NOT NULL DEFAULT 0,
    discount_percent    DECIMAL(5,2) NOT NULL DEFAULT 0,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ------------------------------------------------------------
-- order_schema (order-service)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_schema.orders (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT,
    customer_name   VARCHAR(100) NOT NULL,
    customer_email  VARCHAR(200),
    total_amount    DECIMAL(12,2) NOT NULL DEFAULT 0,
    status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_user ON order_schema.orders(user_id);

CREATE TABLE IF NOT EXISTS order_schema.order_items (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT NOT NULL REFERENCES order_schema.orders(id),
    product_id  BIGINT NOT NULL,
    product_name VARCHAR(200),
    quantity    INT NOT NULL,
    unit_price  DECIMAL(12,2) NOT NULL,
    subtotal    DECIMAL(12,2) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_schema.order_items(order_id);

CREATE TABLE IF NOT EXISTS order_schema.outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_order_outbox_unpublished ON order_schema.outbox_events(created_at) WHERE published_at IS NULL;

-- ------------------------------------------------------------
-- payment_schema (payment-service)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_schema.payments (
    id                BIGSERIAL PRIMARY KEY,
    order_id          BIGINT NOT NULL,
    amount            DECIMAL(12,2) NOT NULL,
    method            VARCHAR(30) NOT NULL DEFAULT 'CARD',
    status            VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    pg_transaction_id VARCHAR(100),
    settled_at        TIMESTAMP,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payments_order ON payment_schema.payments(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_unsettled ON payment_schema.payments(created_at) WHERE settled_at IS NULL;

CREATE TABLE IF NOT EXISTS payment_schema.payment_logs (
    id          BIGSERIAL PRIMARY KEY,
    payment_id  BIGINT NOT NULL REFERENCES payment_schema.payments(id),
    action      VARCHAR(50) NOT NULL,
    detail      TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payment_logs_payment ON payment_schema.payment_logs(payment_id);

CREATE TABLE IF NOT EXISTS payment_schema.settlement_summary (
    id                       BIGSERIAL PRIMARY KEY,
    period_start             TIMESTAMP NOT NULL,
    period_end               TIMESTAMP NOT NULL,
    payment_count            INT NOT NULL,
    total_amount             DECIMAL(14,2) NOT NULL,
    banking_transfer_status  VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    banking_transfer_ref     VARCHAR(100),
    created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payment_schema.outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payment_outbox_unpublished ON payment_schema.outbox_events(created_at) WHERE published_at IS NULL;

-- ------------------------------------------------------------
-- shipping_schema (shipping-service)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shipping_schema.shipments (
    id                  BIGSERIAL PRIMARY KEY,
    order_id            BIGINT NOT NULL UNIQUE,
    status              VARCHAR(20) NOT NULL DEFAULT 'CREATED',
    tracking_number     VARCHAR(50),
    carrier             VARCHAR(50) NOT NULL DEFAULT 'CJ대한통운',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipping_schema.shipments(status);

CREATE TABLE IF NOT EXISTS shipping_schema.shipment_events (
    id              BIGSERIAL PRIMARY KEY,
    shipment_id     BIGINT NOT NULL REFERENCES shipping_schema.shipments(id),
    status          VARCHAR(20) NOT NULL,
    occurred_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_shipment_events_shipment ON shipping_schema.shipment_events(shipment_id);

CREATE TABLE IF NOT EXISTS shipping_schema.outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_shipping_outbox_unpublished ON shipping_schema.outbox_events(created_at) WHERE published_at IS NULL;
