CREATE SCHEMA IF NOT EXISTS product_schema;

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
