-- core-banking 대량 시드 (Oracle). init.sql 로드 후 단독 실행 가능.
-- 계좌 수백~수천 + 과거 이체/원장 레코드 수만 행을 90일 diurnal(시간대별 활동) 분포로 생성한다.
--
-- 재현성: DBMS_RANDOM.SEED(42) 로 고정. 단, Oracle 도 병렬 실행 시 순서가 완전히 결정론적이지
-- 않을 수 있어 "재현 가능하되 바이트 단위로 동일하지는 않음"이 정확한 표현이다(commerce seed-all.sql 과 동일 주의).
--
-- 함정 주의: 비상관(non-lateral) 스칼라 서브쿼리로 "랜덤 계좌 1건 선택"을 시도하면 Oracle 이
-- 결과를 캐싱해 모든 생성 행이 같은 계좌로 몰릴 수 있다(commerce seed-all.sql 이 Postgres LATERAL
-- 의 uncorrelated-subquery 재사용 함정을 문서화한 것과 동일한 계열의 문제). 이를 피하기 위해
-- 반드시 CROSS APPLY(Oracle 12c+)로 명시적 상관 서브쿼리를 사용한다 — CROSS APPLY 는 정의상
-- 외부 행마다 재평가되므로 캐싱 트랩이 발생하지 않는다.

BEGIN
    DBMS_RANDOM.SEED(42);
END;
/

-- ============================================================
-- 1) 대량 계좌 생성: ACC-5001 ~ ACC-5987 (987건, 기존 13건 + = 총 1000 계좌)
--    2% 는 FROZEN 으로 만들어 상태 다양성을 확보(이체 검증/실패 경로 시드).
-- ============================================================
INSERT INTO accounts (id, holder, balance, status)
SELECT
    'ACC-5' || LPAD(TO_CHAR(LEVEL), 3, '0'),
    '고객' || LPAD(TO_CHAR(LEVEL), 4, '0'),
    ROUND(DBMS_RANDOM.VALUE(10000, 8000000), 2),
    CASE WHEN DBMS_RANDOM.VALUE(0, 1) < 0.02 THEN 'FROZEN' ELSE 'ACTIVE' END
FROM dual
CONNECT BY LEVEL <= 987;

COMMIT;

-- ============================================================
-- 2) 과거 이체 20,000건: 최근 90일, 시간대별 가중 분포(diurnal).
--    시간대 가중치(0~23시, 새벽 낮음/주간·저녁 높음, commerce seed-all.sql 과 동일 형태의
--    24구간 가중치를 누적합 CASE 로 구현 — Oracle WIDTH_BUCKET 은 등간격만 지원해
--    Postgres 의 임의 경계 배열 트릭을 그대로 쓸 수 없으므로 누적합 CASE 로 대체).
--    가중치 원본: [1,1,1,1,1,2,3,5,6,7,8,8,7,7,7,8,8,9,8,7,6,4,3,2] (합계 120)
-- ============================================================
INSERT INTO transfers (transfer_ref, from_account, to_account, amount, status, order_id, created_at)
SELECT
    SYS_GUID(),
    fa.id,
    ta.id,
    ROUND(DBMS_RANDOM.VALUE(1000, 300000), 2),
    CASE WHEN DBMS_RANDOM.VALUE(0, 1) < 0.03 THEN 'FAILED' ELSE 'COMPLETED' END,
    NULL,
    TRUNC(SYSDATE) - TRUNC(DBMS_RANDOM.VALUE(0, 90))
        + NUMTODSINTERVAL(
            CASE
                WHEN hv < 1   THEN 0  WHEN hv < 2   THEN 1  WHEN hv < 3   THEN 2
                WHEN hv < 4   THEN 3  WHEN hv < 5    THEN 4  WHEN hv < 7   THEN 5
                WHEN hv < 10  THEN 6  WHEN hv < 15   THEN 7  WHEN hv < 21  THEN 8
                WHEN hv < 28  THEN 9  WHEN hv < 36   THEN 10 WHEN hv < 44  THEN 11
                WHEN hv < 51  THEN 12 WHEN hv < 58   THEN 13 WHEN hv < 65  THEN 14
                WHEN hv < 73  THEN 15 WHEN hv < 81   THEN 16 WHEN hv < 90  THEN 17
                WHEN hv < 98  THEN 18 WHEN hv < 105  THEN 19 WHEN hv < 111 THEN 20
                WHEN hv < 115 THEN 21 WHEN hv < 118  THEN 22 ELSE 23
            END, 'HOUR')
        + NUMTODSINTERVAL(TRUNC(DBMS_RANDOM.VALUE(0, 60)), 'MINUTE')
        + NUMTODSINTERVAL(TRUNC(DBMS_RANDOM.VALUE(0, 60)), 'SECOND')
FROM (SELECT LEVEL AS n, DBMS_RANDOM.VALUE(0, 120) AS hv FROM dual CONNECT BY LEVEL <= 20000) gen
-- 함정 회피: CROSS APPLY 서브쿼리가 gen 의 컬럼을 전혀 참조하지 않으면 Oracle 옵티마이저가
-- "실질적으로 비상관"으로 판단해 1회만 평가하고 재사용해버린다(실측으로 확인된 버그 —
-- 최초 구현은 전 행이 동일 계좌로 몰렸다). gen.n 을 더미로 SELECT 에 끼워 넣어 각 외부 행마다
-- 강제로 재평가되게 한다(commerce seed-all.sql 의 `gs.n AS corr_col` 트릭과 동일 계열의 해법).
CROSS APPLY (
    SELECT id, gen.n AS corr_col FROM accounts WHERE status = 'ACTIVE'
    ORDER BY DBMS_RANDOM.VALUE
    FETCH FIRST 1 ROW ONLY
) fa
CROSS APPLY (
    SELECT id, gen.n AS corr_col FROM accounts WHERE status = 'ACTIVE' AND id <> fa.id
    ORDER BY DBMS_RANDOM.VALUE
    FETCH FIRST 1 ROW ONLY
) ta;

COMMIT;

-- ============================================================
-- 3) 완료된(COMPLETED) 이체마다 원장 복식부기 2건(DEBIT/CREDIT) 생성.
--    주의: 시드된 과거 계좌 잔액은 이 과거 이체 이력과 소급 정합시키지 않는다
--    (테스트베드 목적상 조회/집계 다양성이 핵심 — 잔액 재계산은 범위 밖, 최종 보고에 명시).
-- ============================================================
INSERT INTO ledger_entries (transfer_ref, account_id, direction, amount, created_at)
SELECT t.transfer_ref, t.from_account, 'DEBIT', t.amount, t.created_at
FROM transfers t
WHERE t.status = 'COMPLETED'
  AND NOT EXISTS (
      SELECT 1 FROM ledger_entries le WHERE le.transfer_ref = t.transfer_ref AND le.direction = 'DEBIT'
  );

INSERT INTO ledger_entries (transfer_ref, account_id, direction, amount, created_at)
SELECT t.transfer_ref, t.to_account, 'CREDIT', t.amount, t.created_at
FROM transfers t
WHERE t.status = 'COMPLETED'
  AND NOT EXISTS (
      SELECT 1 FROM ledger_entries le WHERE le.transfer_ref = t.transfer_ref AND le.direction = 'CREDIT'
  );

COMMIT;
