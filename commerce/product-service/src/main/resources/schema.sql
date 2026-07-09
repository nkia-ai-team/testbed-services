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
