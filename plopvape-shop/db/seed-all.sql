-- 통합 시딩 스크립트 (K3s InitContainer용)
-- 스키마 생성
CREATE SCHEMA IF NOT EXISTS order_schema;
CREATE SCHEMA IF NOT EXISTS product_schema;
CREATE SCHEMA IF NOT EXISTS inventory_schema;
CREATE SCHEMA IF NOT EXISTS payment_schema;

-- 카테고리
INSERT INTO product_schema.categories (id, name, description) VALUES
(1, '기기', '전자담배 본체, 팟 디바이스, 모드 기기'),
(2, '액상', '니코틴 액상, 무니코틴 액상, 프리미엄 리퀴드'),
(3, '코일/팟', '교체용 코일, 팟 카트리지, 메쉬 코일'),
(4, '악세서리', '배터리, 충전기, 드립팁, 케이스')
ON CONFLICT (id) DO NOTHING;

-- 상품 16개
INSERT INTO product_schema.products (id, category_id, name, description, price) VALUES
(1, 1, 'PlopVape Air Pro', '초경량 팟 디바이스, 850mAh 배터리, Type-C 충전', 45000),
(2, 1, 'PlopVape Box Mod 200W', '듀얼 배터리 모드, 온도 제어, OLED 디스플레이', 89000),
(3, 1, 'PlopVape Stick V2', '올인원 스틱형, 초보자용, 1100mAh', 32000),
(4, 1, 'PlopVape Pod Mini', '미니 팟 디바이스, 자동 흡입 감지, 휴대용', 28000),
(5, 2, '클래식 버지니아 30ml', '진한 버지니아 블렌드, 니코틴 9mg', 15000),
(6, 2, '아이스 멘솔 50ml', '쿨링 멘솔, 니코틴 6mg, 대용량', 22000),
(7, 2, '망고 패션프루트 30ml', '열대과일 블렌드, 니코틴 3mg', 18000),
(8, 2, '카페라떼 30ml', '커피+크림 디저트 라인, 무니코틴', 16000),
(9, 3, 'Air Pro 교체 팟 (4입)', 'Air Pro 전용, 1.0ohm 메쉬 코일 내장', 12000),
(10, 3, 'Box Mod 메쉬 코일 (5입)', '0.15ohm 메쉬 코일, 고출력용', 18000),
(11, 3, 'Stick V2 교체 코일 (3입)', '0.8ohm MTL 코일, 입호흡용', 9000),
(12, 3, 'Pod Mini 카트리지 (3입)', '1.2ohm 세라믹 코일, 액상 누수 방지', 10000),
(13, 4, '18650 배터리 2개입', '3000mAh 고방전 배터리, Box Mod 호환', 16000),
(14, 4, 'USB-C 고속 충전 케이블', '1.5m 길이, 전 기기 호환', 8000),
(15, 4, '가죽 파우치 케이스', '기기+액상 수납, PlopVape 로고 각인', 25000),
(16, 4, '레진 드립팁 세트 (3종)', '510 규격, 핸드메이드 레진', 15000)
ON CONFLICT (id) DO NOTHING;

-- 재고
INSERT INTO inventory_schema.inventory (id, product_id, stock, reserved) VALUES
(1, 1, 50, 0), (2, 2, 30, 0), (3, 3, 80, 0), (4, 4, 100, 0),
(5, 5, 200, 0), (6, 6, 150, 0), (7, 7, 180, 0), (8, 8, 120, 0),
(9, 9, 300, 0), (10, 10, 250, 0), (11, 11, 400, 0), (12, 12, 350, 0),
(13, 13, 100, 0), (14, 14, 500, 0), (15, 15, 60, 0), (16, 16, 200, 0)
ON CONFLICT (id) DO NOTHING;
