-- 시드(설계 §2-3 데이터 크기: 민원 200만, 처리 이력 약 600만, 사용자 5만, 부서 300).
-- 같은 파일과 같은 식으로 만들므로 T 와 S 의 행 내용이 같다(시각만 설치한 날 기준으로 움직인다).
-- 난수 대신 행 번호의 산술로 값을 고른다(다시 돌려도 같은 값).
-- psql -v app_user=<앱 계정> 으로 돌린다. 담당자 계정의 비밀번호는 합성 시드 값(Passw0rd!)이고 기존 도메인 시드와 같은 관례다.
\set ON_ERROR_STOP 1
-- 민원 수. 설치는 늘 기본값(200만)을 쓴다. 다른 값은 SQL 문법 시험에서만 준다(-v scale=N).
\if :{?scale}
\else
\set scale 2000000
\endif
SET ROLE :"app_user";
SET synchronous_commit = off;
SET maintenance_work_mem = '512MB';

BEGIN;

INSERT INTO department (dept_id, name, parent_id)
SELECT d,
       (ARRAY['민원행정과', '도로관리과', '교통행정과', '환경정책과', '청소행정과', '건축과', '주택과', '공원녹지과',
              '복지정책과', '보건위생과', '세무과', '지역경제과', '문화체육과', '교육지원과', '안전총괄과'])[1 + (d - 1) % 15]
           || ' ' || (1 + (d - 1) / 15) || '팀',
       CASE WHEN d > 15 THEN 1 + (d - 1) % 15 END
FROM generate_series(1, 300) AS d;

INSERT INTO app_user (user_id, login_id, password_hash, name, role, dept_id, created_at)
SELECT u,
       login,
       encode(sha256(convert_to(login || ':Passw0rd!', 'UTF8')), 'hex'),
       '사용자' || u,
       CASE WHEN u <= 49700 THEN 'CITIZEN' ELSE 'STAFF' END,
       CASE WHEN u > 49700 THEN u - 49700 END,
       now() - ((u * 37) % 1500) * interval '1 day'
FROM (SELECT u, CASE WHEN u <= 49700 THEN 'citizen' || lpad(u::text, 5, '0')
                     ELSE 'staff' || lpad((u - 49700)::text, 3, '0') END AS login
      FROM generate_series(1, 50000) AS u) AS s;

-- 큰 수를 곱하는 두 식은 bigint 로 센다(정수로 세면 i 가 약 2만을 넘을 때 넘쳐 200만 시드가 실패한다).
-- 민원: 지난 730일에 고르게, 낮 시간에 몰리게 퍼뜨린다. 번호가 작을수록 오래됐다.
INSERT INTO petition (petition_id, receipt_no, user_id, dept_id, category, title, body, status, is_public, created_at, updated_at)
SELECT i,
       'CP' || lpad(i::text, 8, '0'),
       1 + (i::bigint * 7919) % 49700,
       1 + (i::bigint * 104729) % 300,
       (ARRAY['road', 'traffic', 'environment', 'building', 'welfare', 'tax', 'sanitation', 'etc'])[1 + i % 8],
       (ARRAY['보도블록 파손', '가로등 고장', '불법 주정차', '소음 피해', '쓰레기 무단투기', '악취 발생', '도로 포트홀',
              '버스 정류장 시설', '공원 시설물 파손', '하수구 막힘', '불법 현수막', '건축 공사 소음', '누수 신고',
              '어린이 보호구역', '횡단보도 신호', '자전거 도로 정비', '경로당 시설', '체납 문의', '세금 고지 문의',
              '복지 급여 문의', '방역 요청', '노점 정비', '공사 먼지', '교통 혼잡', '주차장 이용', '도로 결빙',
              '배수로 정비', '나무 가지치기', '야간 소음', '공용 화장실'])[1 + (i * 31) % 30]
           || ' (' || (ARRAY['중앙동', '북부동', '남부동', '동부동', '서부동', '신도심', '구도심', '산업단지',
                             '대학가', '역세권'])[1 + (i * 17) % 10] || ')',
       '안녕하십니까. ' || repeat('현장 확인 후 조치하여 주시기 바랍니다. 주민 불편이 계속되고 있습니다. ', 2 + i % 9),
       CASE WHEN c < now() - interval '30 days' THEN
                CASE WHEN i % 40 = 0 THEN 'IN_PROGRESS' ELSE 'COMPLETED' END
            ELSE (ARRAY['RECEIVED', 'IN_PROGRESS', 'COMPLETED'])[1 + i % 3] END,
       i % 10 < 6,
       c,
       c + ((i % 9) + 1) * interval '1 day'
FROM (SELECT i,
             now() - interval '730 days'
                 + (i::double precision / :scale) * interval '729 days'
                 + ((ARRAY[0, 0, 1, 2, 4, 6, 8, 9, 9, 9, 8, 7, 7, 8, 9, 9, 8, 7, 6, 5, 4, 3, 2, 1])[1 + (i * 13) % 24]) * interval '1 hour'
                 + ((i * 7) % 60) * interval '1 minute' AS c
      FROM generate_series(1, :scale) AS i) AS s;

-- 처리 이력: 접수 1, 처리 중 1, 완료 1(상태까지), 완료 건 가운데 일부는 보완 요청 1.
INSERT INTO petition_history (petition_id, from_status, to_status, actor_id, note, created_at)
SELECT p.petition_id,
       (ARRAY[NULL, 'RECEIVED', 'IN_PROGRESS', 'IN_PROGRESS'])[step],
       (ARRAY['RECEIVED', 'IN_PROGRESS', 'IN_PROGRESS', 'COMPLETED'])[step],
       CASE WHEN step = 1 THEN p.user_id ELSE 49700 + p.dept_id END,
       (ARRAY['submitted', 'accepted', 'supplement requested', 'answered'])[step],
       p.created_at + (step - 1) * interval '1 day'
FROM petition p
JOIN LATERAL generate_series(1, 4) AS step ON
     step = 1
  OR (step = 2 AND p.status IN ('IN_PROGRESS', 'COMPLETED'))
  OR (step = 3 AND p.status = 'COMPLETED' AND p.petition_id % 20 = 0)
  OR (step = 4 AND p.status = 'COMPLETED');

INSERT INTO petition_answer (petition_id, staff_id, body, answered_at)
SELECT petition_id, 49700 + dept_id, '민원 내용을 확인하여 조치를 완료하였습니다. 불편을 드려 죄송합니다.', updated_at
FROM petition WHERE status = 'COMPLETED';

INSERT INTO petition_attachment (petition_id, file_name, size_bytes, created_at)
SELECT petition_id, 'photo_' || petition_id || '.jpg', 50000 + (petition_id * 97) % 900000, created_at
FROM petition WHERE petition_id % 10 < 3;

INSERT INTO stats_dept_monthly (dept_id, month, received, completed, avg_days, refreshed_at)
SELECT dept_id, date_trunc('month', created_at)::date, count(*),
       count(*) FILTER (WHERE status = 'COMPLETED'),
       round(avg(extract(epoch FROM updated_at - created_at) / 86400) FILTER (WHERE status = 'COMPLETED'), 2),
       now()
FROM petition GROUP BY 1, 2;

SELECT setval(pg_get_serial_sequence('petition', 'petition_id'), :scale);
SELECT setval('petition_receipt_seq', :scale);

-- 통계는 같은 트랜잭션 안에서 만든다(커밋 뒤 자동 vacuum 과 잠금을 다투지 않게).
ANALYZE department, app_user, petition, petition_history, petition_answer, petition_attachment, stats_dept_monthly;

COMMIT;

RESET ROLE;
