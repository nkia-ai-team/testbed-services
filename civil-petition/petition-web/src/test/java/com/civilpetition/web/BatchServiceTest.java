package com.civilpetition.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;

/** 보관 배치의 건수 상한, 묶음 크기, 배치 시간 창(검토 세션 Q21 답 (나) 의 조건 ①, ②). */
class BatchServiceTest {

    // 2026-11-17 02:20 KST = 2026-11-16 17:20 UTC (was02 타이머 시각).
    static final Instant NIGHT = Instant.parse("2026-11-16T17:20:00Z");
    // 2026-11-17 14:00 KST.
    static final Instant AFTERNOON = Instant.parse("2026-11-17T05:00:00Z");

    static ArchivePolicy policy(long keepRows, int chunkRows) {
        return ArchivePolicy.of(keepRows, chunkRows, 1000, "02:00-05:00", "Asia/Seoul");
    }

    /** 쉼마다 시계를 쉰 만큼 앞으로 민다. */
    static final class MovingClock extends Clock {
        Instant now;

        MovingClock(Instant start) {
            now = start;
        }

        @Override
        public ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return now;
        }
    }

    static JdbcTemplate jdbc(long rows) {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        when(jdbc.queryForObject(BatchService.LOCK_SQL, Boolean.class)).thenReturn(true);
        when(jdbc.queryForObject(BatchService.COUNT_SQL, Long.class)).thenReturn(rows);
        when(jdbc.queryForObject(org.mockito.ArgumentMatchers.startsWith(MOVE_HEAD), eq(Integer.class)))
                .thenAnswer(call -> sizeOf(call.getArgument(0)));
        return jdbc;
    }

    static final String MOVE_HEAD = BatchService.MOVE_SQL.substring(0, BatchService.MOVE_SQL.indexOf("%d"));

    static int sizeOf(String sql) {
        assertThat(sql).isEqualTo(BatchService.moveSql(Integer.parseInt(sql.substring(MOVE_HEAD.length()).split("\\)")[0])));
        return Integer.parseInt(sql.substring(MOVE_HEAD.length()).split("\\)")[0]);
    }

    static List<Integer> chunks(JdbcTemplate jdbc, int calls) {
        List<Integer> sizes = new ArrayList<>();
        org.mockito.ArgumentCaptor<String> sql = org.mockito.ArgumentCaptor.forClass(String.class);
        verify(jdbc, times(calls)).queryForObject(sql.capture(), eq(Integer.class));
        for (String value : sql.getAllValues()) {
            sizes.add(sizeOf(value));
        }
        return sizes;
    }

    @Test
    void movesOnlyTheRowsAboveTheCapInChunksNoLargerThanTheChunkSize() {
        JdbcTemplate jdbc = jdbc(2_002_500);
        MovingClock clock = new MovingClock(NIGHT);
        List<Long> pauses = new ArrayList<>();
        BatchService service = new BatchService(jdbc, policy(2_000_000, 1000), clock, millis -> {
            pauses.add(millis);
            clock.now = clock.now.plusMillis(millis);
        });

        BatchService.Outcome outcome = service.archive();

        assertThat(outcome).isEqualTo(new BatchService.Outcome(2500, "ok"));
        assertThat(chunks(jdbc, 3)).containsExactly(1000, 1000, 500);
        assertThat(pauses).containsExactly(1000L, 1000L);
    }

    @Test
    void doesNothingAtOrBelowTheCap() {
        for (long rows : new long[] {2_000_000, 1_999_000}) {
            JdbcTemplate jdbc = jdbc(rows);
            BatchService service = new BatchService(jdbc, policy(2_000_000, 1000), new MovingClock(NIGHT), millis -> {
            });

            assertThat(service.archive()).isEqualTo(new BatchService.Outcome(0, "ok"));
            verify(jdbc, never()).queryForObject(org.mockito.ArgumentMatchers.startsWith(MOVE_HEAD), eq(Integer.class));
        }
    }

    @Test
    void outsideTheNightWindowItDoesNotEvenCount() {
        JdbcTemplate jdbc = jdbc(3_000_000);
        BatchService service = new BatchService(jdbc, policy(2_000_000, 1000), new MovingClock(AFTERNOON), millis -> {
        });

        assertThat(service.archive()).isEqualTo(new BatchService.Outcome(0, "skipped"));
        verify(jdbc, never()).queryForObject(BatchService.COUNT_SQL, Long.class);
        verify(jdbc, never()).queryForObject(org.mockito.ArgumentMatchers.startsWith(MOVE_HEAD), eq(Integer.class));
    }

    @Test
    void stopsWhenTheWindowClosesAndLeavesTheRestForTheNextNight() {
        JdbcTemplate jdbc = jdbc(2_010_000);
        // 04:59:58 KST 에 시작, 쉼 1초마다 시계가 1초 간다: 두 묶음 뒤 05:00:00 에 창이 닫힌다.
        MovingClock clock = new MovingClock(Instant.parse("2026-11-16T19:59:58Z"));
        BatchService service = new BatchService(jdbc, policy(2_000_000, 1000), clock,
                millis -> clock.now = clock.now.plus(Duration.ofMillis(millis)));

        assertThat(service.archive()).isEqualTo(new BatchService.Outcome(2000, "partial"));
        assertThat(chunks(jdbc, 2)).containsExactly(1000, 1000);
    }

    @Test
    void stopsWhenNothingIsLeftToMove() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        when(jdbc.queryForObject(BatchService.LOCK_SQL, Boolean.class)).thenReturn(true);
        when(jdbc.queryForObject(BatchService.COUNT_SQL, Long.class)).thenReturn(2_005_000L);
        when(jdbc.queryForObject(org.mockito.ArgumentMatchers.startsWith(MOVE_HEAD), eq(Integer.class))).thenReturn(1000, 0);
        BatchService service = new BatchService(jdbc, policy(2_000_000, 1000), new MovingClock(NIGHT), millis -> {
        });

        assertThat(service.archive()).isEqualTo(new BatchService.Outcome(1000, "ok"));
    }

    @Test
    void aSecondRunWhileOneHoldsTheLockDoesNothing() {
        JdbcTemplate jdbc = jdbc(3_000_000);
        when(jdbc.queryForObject(BatchService.LOCK_SQL, Boolean.class)).thenReturn(false);
        BatchService service = new BatchService(jdbc, policy(2_000_000, 1000), new MovingClock(NIGHT), millis -> {
        });

        assertThat(service.archive()).isEqualTo(new BatchService.Outcome(0, "busy"));
        verify(jdbc, never()).queryForObject(BatchService.COUNT_SQL, Long.class);
        verify(jdbc, never()).queryForObject(org.mockito.ArgumentMatchers.startsWith(MOVE_HEAD), eq(Integer.class));
        verify(jdbc, never()).queryForObject(BatchService.UNLOCK_SQL, Boolean.class);
    }

    @Test
    void theLockIsReleasedAfterTheRunAndAfterAFailure() {
        JdbcTemplate ok = jdbc(2_001_000);
        new BatchService(ok, policy(2_000_000, 1000), new MovingClock(NIGHT), millis -> {
        }).archive();
        org.mockito.InOrder order = org.mockito.Mockito.inOrder(ok);
        order.verify(ok).queryForObject(BatchService.LOCK_SQL, Boolean.class);
        order.verify(ok).queryForObject(BatchService.COUNT_SQL, Long.class);
        order.verify(ok).queryForObject(BatchService.UNLOCK_SQL, Boolean.class);

        JdbcTemplate failing = jdbc(2_001_000);
        when(failing.queryForObject(org.mockito.ArgumentMatchers.startsWith(MOVE_HEAD), eq(Integer.class)))
                .thenThrow(new org.springframework.dao.QueryTimeoutException("statement timeout"));
        BatchService service = new BatchService(failing, policy(2_000_000, 1000), new MovingClock(NIGHT), millis -> {
        });
        assertThatThrownBy(service::archive).isInstanceOf(org.springframework.dao.QueryTimeoutException.class);
        verify(failing).queryForObject(BatchService.UNLOCK_SQL, Boolean.class);
    }

    @Test
    void lockAndWorkShareOneBorrowedConnection() throws Exception {
        JdbcTemplate pool = mock(JdbcTemplate.class);
        java.sql.Connection borrowed = mock(java.sql.Connection.class);
        when(pool.execute(any(org.springframework.jdbc.core.ConnectionCallback.class))).thenAnswer(call ->
                ((org.springframework.jdbc.core.ConnectionCallback<?>) call.getArgument(0)).doInConnection(borrowed));
        List<JdbcTemplate> seen = new ArrayList<>();

        BatchService.Outcome outcome = BatchService.pinned(pool).run(one -> {
            seen.add(one);
            return new BatchService.Outcome(0, "ok");
        });

        assertThat(outcome.result()).isEqualTo("ok");
        assertThat(seen).hasSize(1);
        assertThat(seen.get(0).getDataSource()).isInstanceOf(org.springframework.jdbc.datasource.SingleConnectionDataSource.class);
        org.springframework.jdbc.datasource.SingleConnectionDataSource single =
                (org.springframework.jdbc.datasource.SingleConnectionDataSource) seen.get(0).getDataSource();
        assertThat(((org.springframework.jdbc.datasource.ConnectionProxy) single.getConnection()).getTargetConnection()).isSameAs(borrowed);
    }

    @Test
    void runRecordsTheResultOfTheJob() {
        JdbcTemplate jdbc = jdbc(3_000_000);
        BatchService service = new BatchService(jdbc, policy(2_000_000, 1000), new MovingClock(AFTERNOON), millis -> {
        });

        assertThat(service.run("archive")).containsEntry("result", "skipped").containsEntry("rows", 0L);
        verify(jdbc).update(anyString(), eq("archive"), any(), eq(0L), eq("skipped"));
    }

    @Test
    void theWindowIsReadInItsOwnZoneAndMayWrapPastMidnight() {
        ArchivePolicy night = policy(1, 1);
        assertThat(night.open(Instant.parse("2026-11-16T17:00:00Z"))).isTrue();   // 02:00 KST
        assertThat(night.open(Instant.parse("2026-11-16T16:59:59Z"))).isFalse();  // 01:59:59 KST
        assertThat(night.open(Instant.parse("2026-11-16T20:00:00Z"))).isFalse();  // 05:00 KST
        ArchivePolicy wrap = ArchivePolicy.of(1, 1, 0, "23:00-01:00", "Asia/Seoul");
        assertThat(wrap.open(Instant.parse("2026-11-16T14:30:00Z"))).isTrue();    // 23:30 KST
        assertThat(wrap.open(Instant.parse("2026-11-16T15:30:00Z"))).isTrue();    // 00:30 KST
        assertThat(wrap.open(Instant.parse("2026-11-16T16:30:00Z"))).isFalse();   // 01:30 KST
    }

    @Test
    void shippedDefaultsKeepTheSeedSizeAndCoverTheNightTimer() throws Exception {
        java.util.Properties shipped = org.springframework.core.io.support.PropertiesLoaderUtils.loadProperties(
                new org.springframework.core.io.ClassPathResource("application.properties"));
        ArchivePolicy defaults = ArchivePolicy.of(
                Long.parseLong(shipped.getProperty("petition.archive.keep-rows")),
                Integer.parseInt(shipped.getProperty("petition.archive.chunk-rows")),
                Long.parseLong(shipped.getProperty("petition.archive.pause-ms")),
                shipped.getProperty("petition.archive.window"),
                shipped.getProperty("petition.archive.zone"));
        // 상한은 시드 크기(db/seed.sql 의 민원 200만)와 같다.
        assertThat(defaults.keepRows()).isEqualTo(2_000_000L);
        assertThat(defaults.chunkRows()).isLessThanOrEqualTo(1000);
        // was02 타이머(petition-archive.timer 02:20 KST)가 창 안이고, 낮 피크(16시)는 창 밖이다.
        assertThat(defaults.open(NIGHT)).isTrue();
        assertThat(defaults.open(Instant.parse("2026-11-17T07:00:00Z"))).isFalse();
    }

    @Test
    void theChunkSizeGoesIntoTheStatementAsALiteralSoThePlanKnowsIt() {
        assertThat(BatchService.moveSql(1000)).contains("LIMIT 1000)").doesNotContain("?").doesNotContain("%d");
        assertThatThrownBy(() -> BatchService.moveSql(0)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> BatchService.moveSql(ArchivePolicy.MAX_CHUNK_ROWS + 1)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void rejectsSettingsThatWouldMoveEverythingAtOnceOrNever() {
        assertThatThrownBy(() -> policy(0, 1000)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> policy(2_000_000, 0)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> policy(2_000_000, ArchivePolicy.MAX_CHUNK_ROWS + 1)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> ArchivePolicy.of(1, 1, 0, "02:00-02:00", "Asia/Seoul")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> ArchivePolicy.of(1, 1, 0, "0200", "Asia/Seoul")).isInstanceOf(IllegalArgumentException.class);
    }
}
