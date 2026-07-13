-- product-service 시드(id 1~16)와 동일한 product_id 키를 사용한다(cross-service FK 아님, 값만 일치).
INSERT INTO pricing_schema.prices (id, product_id, base_price) VALUES
(1, 1, 45000), (2, 2, 89000), (3, 3, 32000), (4, 4, 28000),
(5, 5, 15000), (6, 6, 22000), (7, 7, 18000), (8, 8, 16000),
(9, 9, 12000), (10, 10, 18000), (11, 11, 9000), (12, 12, 10000),
(13, 13, 16000), (14, 14, 8000), (15, 15, 25000), (16, 16, 15000)
ON CONFLICT (id) DO NOTHING;

INSERT INTO pricing_schema.promotions (id, name, description, discount_percent, starts_at, ends_at, active) VALUES
(1, '봄맞이 세일', '전 품목 10% 할인', 10.00, '2026-01-01 00:00:00', '2026-12-31 23:59:59', true),
(2, '신규가입 이벤트', '가입 첫 구매 5% 추가 할인', 5.00, '2026-01-01 00:00:00', '2026-12-31 23:59:59', true),
(3, '겨울 시즌 종료 세일', '재고 정리 15% 할인', 15.00, '2025-01-01 00:00:00', '2025-02-28 23:59:59', false)
ON CONFLICT (id) DO NOTHING;

INSERT INTO pricing_schema.coupons (id, code, discount_amount, discount_percent, active) VALUES
(1, 'WELCOME5000', 5000, 0, true),
(2, 'SAVE10', 0, 10.00, true),
(3, 'VIP20', 0, 20.00, true),
(4, 'EXPIRED1000', 1000, 0, false)
ON CONFLICT (id) DO NOTHING;
