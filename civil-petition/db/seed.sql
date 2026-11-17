-- 시드(설계 §2-3 데이터 크기: 민원 200만, 처리 이력 약 450만, 사용자 5만, 부서 300).
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
-- 민원: 시드가 곧 정상 상태가 되게 만든다(설계 §2-3, 검토 세션 Q21 답 ①, Q22 답의 시험 조건).
--   접수 시각: 부하기의 시간별 신청 수(loadgen/entrypoint.sh 하루 표의 시간별 목표 요청률 x 3600 x 신청 8%, KST 시각)
--     그대로 바로 앞 시간부터 거꾸로 채운다. 200만이면 약 16.4일(보존 건수 상한 / 하루 신청 121,824)이다.
--     번호가 작을수록 오래됐다(i 가 클수록 최근, 접수 시각과 번호 순서가 같다).
--   처리 상태: 부하기 여정(신청 8%, 담당자 처리 10%, 처리는 그 부서 열린 건 가운데 가장 오래된 10건에서 하나,
--     접수 상태면 25% 처리 중, 나머지 답변 완료)을 모의한 정상 상태 꼴이다. 열린 건은 가장 최근 1%(200만이면 2만,
--     모의 7~16일째 1.9만~2.8만), 열린 건 가운데 3% 가 처리 중(모의 약 600), 나머지는 완료다. 완료 시각은 접수에서
--     모의의 처리 지연 분포(20칸, 중앙값 약 3.6시간)만큼 뒤다. 완료 건의 25% 는 처리 중을 거쳤다(모의 0.25).
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
       status,
       i % 10 < 6,
       c,
       CASE status
           WHEN 'COMPLETED' THEN least(c + done_after, now())
           WHEN 'IN_PROGRESS' THEN c + (now() - c) / 2
           ELSE c END
FROM (SELECT i, c,
             CASE WHEN :scale - i >= :scale / 100 THEN 'COMPLETED'
                  WHEN i % 100 < 3 THEN 'IN_PROGRESS'
                  ELSE 'RECEIVED' END AS status,
             (ARRAY[8, 22, 37, 55, 75, 98, 123, 150, 180, 214, 250, 290, 335, 390, 450, 520, 600, 700, 820, 1000])
                 [1 + (i * 37) % 20] * interval '1 minute' AS done_after
      FROM (SELECT (:scale - (h.before + j) + 1)::int AS i,
                   h.hour_start + interval '1 hour' - ((j - 0.5) / h.cnt)::float8 * interval '1 hour' AS c
            FROM (SELECT hour_start, cnt, sum(cnt) OVER (ORDER BY n) - cnt AS before
                  FROM (SELECT n, date_trunc('hour', now()) - (n + 1) * interval '1 hour' AS hour_start,
                               (ARRAY[3168, 2592, 2016, 1728, 1440, 1728, 2016, 2592, 3168, 4032, 5184, 6048,
                                      6912, 7488, 8064, 8640, 8640, 8640, 8064, 7488, 6912, 6048, 5184, 4032])
                                   [1 + extract(hour FROM (date_trunc('hour', now()) - (n + 1) * interval '1 hour')
                                                AT TIME ZONE 'Asia/Seoul')::int] AS cnt
                        FROM generate_series(0, 24 * 730) AS n) AS hours) AS h
            CROSS JOIN LATERAL generate_series(1, least(h.cnt, :scale - h.before)::int) AS j
            WHERE h.before < :scale) AS placed) AS s;

-- 처리 이력: 앱이 쓰는 꼴 그대로. 신청(신청인), 처리 중으로(담당자, 처리 중 건과 완료 건의 25%), 답변 완료(담당자).
INSERT INTO petition_history (petition_id, from_status, to_status, actor_id, note, created_at)
SELECT p.petition_id,
       CASE step WHEN 1 THEN NULL WHEN 2 THEN 'RECEIVED'
                 ELSE CASE WHEN p.status = 'COMPLETED' AND p.petition_id % 4 = 1 THEN 'IN_PROGRESS' ELSE 'RECEIVED' END END,
       (ARRAY['RECEIVED', 'IN_PROGRESS', 'COMPLETED'])[step],
       CASE WHEN step = 1 THEN p.user_id ELSE 49700 + p.dept_id END,
       CASE WHEN step = 1 THEN 'submitted' ELSE '현장 확인 후 조치하였습니다.' END,
       CASE step WHEN 1 THEN p.created_at
                 WHEN 2 THEN CASE WHEN p.status = 'COMPLETED' THEN p.created_at + (p.updated_at - p.created_at) / 2
                                  ELSE p.updated_at END
                 ELSE p.updated_at END
FROM petition p
JOIN LATERAL generate_series(1, 3) AS step ON
     step = 1
  OR (step = 2 AND (p.status = 'IN_PROGRESS' OR (p.status = 'COMPLETED' AND p.petition_id % 4 = 1)))
  OR (step = 3 AND p.status = 'COMPLETED');

INSERT INTO petition_answer (petition_id, staff_id, body, answered_at)
SELECT petition_id, 49700 + dept_id, '현장 확인 후 조치하였습니다.', updated_at
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
