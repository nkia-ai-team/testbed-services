package com.civilpetition.web;

import java.time.Clock;
import java.time.Instant;
import java.util.Map;
import java.util.function.Function;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.ConnectionCallback;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;
import org.springframework.stereotype.Service;

/**
 * 야간 배치(설계 §2-3 배치). was02 의 systemd 타이머가 관리 경로로 부른다.
 * stats: 지난달과 이번 달의 부서별 월간 집계를 다시 만든다.
 * archive: 민원 표의 행 수가 보존 건수 상한을 넘은 만큼 가장 오래된 민원부터 보관 표로 옮긴다
 * (처리 이력도 같은 민원 것을 함께 옮긴다). 작은 묶음으로 나눠 옮기고 묶음 사이에 쉬며,
 * 야간 배치 시간 창 안에서만 돈다. 상한, 묶음 크기, 쉼, 창은 설정(application.properties 의 기본값,
 * 환경변수로 덮음)이다. 두 호출이 겹치면(타이머와 관리 경로 수동 호출, 두 WAS) 같은 초과분을 두 번 옮기므로
 * 한 연결 위의 PostgreSQL 세션 advisory lock 으로 하나만 돌게 하고, 못 잡은 쪽은 busy 로 끝난다. 보존 기준을 나이가 아니라 건수로 둔 까닭은 평시 쓰기가 시드 밀도보다 빨라
 * 나이 기준으로는 표가 몇 달 동안 자라기 때문이다(검토 세션 Q21 답 (나)).
 */
@Service
public class BatchService {

    static final String COUNT_SQL = "SELECT count(*) FROM petition";
    // 가장 오래된 민원 n 건과 그 처리 이력을 보관 표로 옮기고 지운다. 답변과 첨부 메타는 외래 키 cascade 로
    // 함께 지워진다(옮기지 않는다). 여러 번 참조하는 CTE 라 old 는 한 번만 계산된다.
    // 건수는 바인드 변수가 아니라 글자로 넣는다(검증한 int). 바인드 변수면 일반 계획(generic plan)이 건수를 몰라
    // 처리 이력 표 전체를 읽는 해시 조인을 고를 수 있다(200만 시드에서 한 묶음 7초, 글자로 넣으면 0.07초).
    static final String MOVE_SQL = "WITH old AS (SELECT petition_id FROM petition ORDER BY created_at, petition_id LIMIT %d), "
            + "h AS (INSERT INTO petition_history_archive SELECT ph.* FROM petition_history ph "
            + "JOIN old USING (petition_id) RETURNING 1), "
            + "p AS (INSERT INTO petition_archive SELECT pt.*, now() FROM petition pt JOIN old USING (petition_id) RETURNING 1), "
            + "d AS (DELETE FROM petition WHERE petition_id IN (SELECT petition_id FROM old) RETURNING 1) "
            + "SELECT count(*)::int FROM d";

    static String moveSql(int rows) {
        if (rows < 1 || rows > ArchivePolicy.MAX_CHUNK_ROWS) {
            throw new IllegalArgumentException("chunk size out of range: " + rows);
        }
        return String.format(java.util.Locale.ROOT, MOVE_SQL, rows);
    }

    /** 보관 배치 하나만 돌게 하는 advisory lock 열쇠(이 앱 안에서만 쓰는 고정 값). */
    static final long ARCHIVE_LOCK_KEY = 7_301_420_917L;
    static final String LOCK_SQL = "SELECT pg_try_advisory_lock(" + ARCHIVE_LOCK_KEY + ")";
    static final String UNLOCK_SQL = "SELECT pg_advisory_unlock(" + ARCHIVE_LOCK_KEY + ")";

    /** 일을 한 DB 연결 위에서 돌린다. 세션 advisory lock 은 잡은 연결에서만 유지되고 풀어진다. */
    interface OneConnection {
        Outcome run(Function<JdbcTemplate, Outcome> body);
    }

    /** 풀에서 연결 하나를 빌려 그 연결만 쓰는 JdbcTemplate 으로 body 를 돌린다. */
    static OneConnection pinned(JdbcTemplate jdbc) {
        return body -> jdbc.execute((ConnectionCallback<Outcome>) con ->
                body.apply(new JdbcTemplate(new SingleConnectionDataSource(con, true))));
    }

    /** 묶음 사이 쉼. 시험이 시계를 움직이려고 바꿔 끼운다. */
    interface Sleeper {
        void sleep(long millis) throws InterruptedException;
    }

    record Outcome(long rows, String result) {
    }

    private static final Logger log = LoggerFactory.getLogger(BatchService.class);
    private final JdbcTemplate jdbc;
    private final ArchivePolicy policy;
    private final Clock clock;
    private final Sleeper sleeper;
    private final OneConnection connection;

    @Autowired
    public BatchService(JdbcTemplate jdbc,
                        @Value("${petition.archive.keep-rows}") long keepRows,
                        @Value("${petition.archive.chunk-rows}") int chunkRows,
                        @Value("${petition.archive.pause-ms}") long pauseMillis,
                        @Value("${petition.archive.window}") String window,
                        @Value("${petition.archive.zone}") String zone) {
        this(jdbc, ArchivePolicy.of(keepRows, chunkRows, pauseMillis, window, zone), Clock.systemUTC(), Thread::sleep, pinned(jdbc));
    }

    /** 시험용: 모의 JdbcTemplate 하나를 그대로 "한 연결" 로 쓴다. */
    BatchService(JdbcTemplate jdbc, ArchivePolicy policy, Clock clock, Sleeper sleeper) {
        this(jdbc, policy, clock, sleeper, body -> body.apply(jdbc));
    }

    BatchService(JdbcTemplate jdbc, ArchivePolicy policy, Clock clock, Sleeper sleeper, OneConnection connection) {
        this.jdbc = jdbc;
        this.policy = policy;
        this.clock = clock;
        this.sleeper = sleeper;
        this.connection = connection;
    }

    public Map<String, Object> run(String job) {
        long started = System.currentTimeMillis();
        Outcome outcome = switch (job) {
            case "stats" -> new Outcome(refreshStats(), "ok");
            case "archive" -> archive();
            default -> throw new ApiException(org.springframework.http.HttpStatus.NOT_FOUND, "no such job");
        };
        long took = System.currentTimeMillis() - started;
        jdbc.update("INSERT INTO batch_run (job, started_at, finished_at, rows_affected, result) "
                + "VALUES (?, to_timestamp(?::double precision / 1000), now(), ?, ?)", job, started, outcome.rows(), outcome.result());
        log.info("batch job={} result={} rows={} took_ms={}", job, outcome.result(), outcome.rows(), took);
        return Map.of("job", job, "rows", outcome.rows(), "result", outcome.result(), "tookMs", took);
    }

    long refreshStats() {
        return jdbc.update(
                "INSERT INTO stats_dept_monthly (dept_id, month, received, completed, avg_days, refreshed_at) "
                        + "SELECT dept_id, date_trunc('month', created_at)::date, count(*), "
                        + "count(*) FILTER (WHERE status = 'COMPLETED'), "
                        + "round(avg(extract(epoch FROM updated_at - created_at) / 86400) FILTER (WHERE status = 'COMPLETED'), 2), now() "
                        + "FROM petition WHERE created_at >= date_trunc('month', now()) - interval '1 month' "
                        + "GROUP BY 1, 2 "
                        + "ON CONFLICT (dept_id, month) DO UPDATE SET received = EXCLUDED.received, "
                        + "completed = EXCLUDED.completed, avg_days = EXCLUDED.avg_days, refreshed_at = EXCLUDED.refreshed_at");
    }

    /**
     * 결과: ok(상한까지 옮김, 상한 아래면 0 건), skipped(창 밖이라 아무것도 하지 않음, 행 수도 세지 않음),
     * partial(옮기는 중에 창이 닫혀 멈춤. 남은 몫은 다음 밤에 옮긴다), busy(다른 보관 배치가 돌고 있어 아무것도 하지 않음).
     */
    Outcome archive() {
        if (!policy.open(now())) {
            return new Outcome(0, "skipped");
        }
        return connection.run(one -> {
            if (!Boolean.TRUE.equals(one.queryForObject(LOCK_SQL, Boolean.class))) {
                return new Outcome(0, "busy");
            }
            try {
                return moveExcess(one);
            } finally {
                one.queryForObject(UNLOCK_SQL, Boolean.class);
            }
        });
    }

    private Outcome moveExcess(JdbcTemplate one) {
        Long total = one.queryForObject(COUNT_SQL, Long.class);
        long excess = (total == null ? 0 : total) - policy.keepRows();
        long moved = 0;
        while (moved < excess) {
            if (!policy.open(now())) {
                return new Outcome(moved, "partial");
            }
            int size = (int) Math.min(policy.chunkRows(), excess - moved);
            Integer chunk = one.queryForObject(moveSql(size), Integer.class);
            if (chunk == null || chunk == 0) {
                break;
            }
            moved += chunk;
            if (moved < excess && policy.pauseMillis() > 0) {
                try {
                    sleeper.sleep(policy.pauseMillis());
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    return new Outcome(moved, "partial");
                }
            }
        }
        return new Outcome(moved, "ok");
    }

    private Instant now() {
        return clock.instant();
    }
}
