CREATE SCHEMA IF NOT EXISTS pricing_schema;

CREATE TABLE IF NOT EXISTS pricing_schema.prices (
    id          BIGSERIAL PRIMARY KEY,
    product_id  BIGINT NOT NULL UNIQUE,
    base_price  DECIMAL(12,2) NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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

CREATE INDEX IF NOT EXISTS idx_prices_product ON pricing_schema.prices(product_id);
