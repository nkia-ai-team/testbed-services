package com.civilpetition.web;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

/**
 * 야간 배치(설계 §2-3 배치). was02 의 systemd 타이머가 관리 경로로 부른다.
 * stats: 지난달과 이번 달의 부서별 월간 집계를 다시 만든다.
 * archive: 보존 기간(2년)이 지난 민원을 보관 표로 옮긴다(묶음 단위, 한 번에 오래 잠그지 않게).
 */
@Service
public class BatchService {

    static final int ARCHIVE_CHUNK = 5000;
    static final String RETENTION = "730 days";
    private static final Logger log = LoggerFactory.getLogger(BatchService.class);
    private final JdbcTemplate jdbc;

    public BatchService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public Map<String, Object> run(String job) {
        long started = System.currentTimeMillis();
        long rows = switch (job) {
            case "stats" -> refreshStats();
            case "archive" -> archive();
            default -> throw new ApiException(org.springframework.http.HttpStatus.NOT_FOUND, "no such job");
        };
        long took = System.currentTimeMillis() - started;
        jdbc.update("INSERT INTO batch_run (job, started_at, finished_at, rows_affected, result) "
                + "VALUES (?, to_timestamp(?::double precision / 1000), now(), ?, 'ok')", job, started, rows);
        log.info("batch job={} rows={} took_ms={}", job, rows, took);
        return Map.of("job", job, "rows", rows, "tookMs", took);
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

    long archive() {
        long moved = 0;
        while (true) {
            Integer chunk = jdbc.queryForObject(
                    "WITH old AS (SELECT petition_id FROM petition WHERE created_at < now() - interval '" + RETENTION + "' "
                            + "ORDER BY petition_id LIMIT " + ARCHIVE_CHUNK + "), "
                            + "h AS (INSERT INTO petition_history_archive SELECT ph.* FROM petition_history ph "
                            + "JOIN old USING (petition_id) RETURNING 1), "
                            + "p AS (INSERT INTO petition_archive SELECT pt.*, now() FROM petition pt JOIN old USING (petition_id) RETURNING 1), "
                            + "d AS (DELETE FROM petition WHERE petition_id IN (SELECT petition_id FROM old) RETURNING 1) "
                            + "SELECT count(*)::int FROM d",
                    Integer.class);
            if (chunk == null || chunk == 0) {
                return moved;
            }
            moved += chunk;
        }
    }
}
