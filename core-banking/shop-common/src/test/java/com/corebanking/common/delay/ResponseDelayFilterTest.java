package com.corebanking.common.delay;

import org.junit.jupiter.api.Test;
import org.springframework.jdbc.BadSqlGrammarException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

import java.sql.SQLException;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.clearInvocations;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * F21-P 지연 표면의 계약: (1) 행이 지시한 만큼만 늦춘다, (2) 프로브는 늦추지 않는다,
 * (3) 요청 스레드는 DB 를 건드리지 않는다, (4) 제어 표면이 죽어도 홉은 죽지 않고
 * 표면이 스스로를 방송하지도 않는다, (5) 인터럽트는 지연만 취소한다.
 */
class ResponseDelayFilterTest {

    /** 기존 PVC 라 init.sql 이 다시 돌지 않은 테스트베드에서 실제로 나는 예외다. */
    private static BadSqlGrammarException tableMissing() {
        return new BadSqlGrammarException("select", "select delay_ms ...",
                new SQLException("ORA-00942: table or view does not exist"));
    }

    @SuppressWarnings("unchecked")
    private static JdbcTemplate jdbcReturning(Long delayMs) {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        when(jdbc.queryForList(anyString(), any(Class.class), any()))
                .thenReturn(delayMs == null ? List.of() : List.of(delayMs));
        return jdbc;
    }

    @SuppressWarnings("unchecked")
    private static JdbcTemplate jdbcFailing() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        when(jdbc.queryForList(anyString(), any(Class.class), any())).thenThrow(tableMissing());
        return jdbc;
    }

    private static ResponseDelayFilter filterWith(JdbcTemplate jdbc) {
        return new ResponseDelayFilter(jdbc, "transfer", 2000L);
    }

    private static long invoke(ResponseDelayFilter filter, String uri) throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", uri);
        MockFilterChain chain = new MockFilterChain();
        long started = System.nanoTime();
        filter.doFilterInternal(request, new MockHttpServletResponse(), chain);
        long elapsedMs = (System.nanoTime() - started) / 1_000_000L;
        // 체인은 언제나 이어져야 한다 — 지연은 요청을 늦출 뿐 끊지 않는다(slow-not-failed).
        assertNotNull(chain.getRequest(), "filter chain was not continued");
        return elapsedMs;
    }

    @Test
    void delays_the_response_by_the_value_in_the_control_row() throws Exception {
        ResponseDelayFilter filter = filterWith(jdbcReturning(120L));
        filter.refresh();
        assertTrue(invoke(filter, "/api/transfers") >= 100L,
                "control row asked for 120ms but the request was not delayed");
    }

    @Test
    void never_delays_actuator_probes_or_scrapes() {
        // 프로브 timeoutSeconds 는 3 이다. delay_ms>=3000 이 /actuator/health 에 걸리면
        // readiness 가 ~30s, liveness 가 ~75s 만에 파드를 죽여 지연 주입이 파드 장애
        // 주입으로 바뀐다. /actuator/prometheus 는 메트릭 스크레이프까지 같이 늦춘다.
        ResponseDelayFilter filter = filterWith(jdbcReturning(9_000L));
        for (String probe : new String[]{"/actuator/health", "/actuator/health/readiness", "/actuator/prometheus"}) {
            assertTrue(filter.shouldNotFilter(new MockHttpServletRequest("GET", probe)), probe);
        }
        assertFalse(filter.shouldNotFilter(new MockHttpServletRequest("GET", "/api/transfers")));
    }

    @Test
    void request_thread_never_queries_the_control_table() throws Exception {
        // 요청 스레드에서 조회하면 otel 이 서버 스팬의 자식 JDBC 스팬을 붙여
        // "SELECT response_delay_control 직후 같은 길이의 공백"을 자백한다.
        JdbcTemplate jdbc = jdbcReturning(50L);
        ResponseDelayFilter filter = filterWith(jdbc);
        filter.refresh();
        clearInvocations(jdbc);
        invoke(filter, "/api/transfers");
        verifyNoInteractions(jdbc);
    }

    @Test
    void applies_no_delay_and_warns_once_when_the_control_table_is_missing() throws Exception {
        // 제어 표면이 죽어도 앱은 정상이어야 한다. 그리고 2초마다 같은 실패를 방송하면
        // (OTEL_LOGS_EXPORTER=otlp) 표면이 스스로를 드러낸다 — 상태 변화 시 1회만.
        ResponseDelayFilter filter = filterWith(jdbcFailing());
        for (int i = 0; i < 5; i++) {
            filter.refresh();
        }
        assertEquals(0L, filter.currentDelayMs());
        assertEquals(1, filter.unavailableTransitions(), "the surface broadcast itself every poll");
        assertTrue(invoke(filter, "/api/transfers") < 100L);
    }

    @Test
    @SuppressWarnings("unchecked")
    void warns_again_only_after_the_control_surface_recovers_and_fails_anew() {
        // 표면이 살아났다 다시 죽으면 그건 새 사실이므로 한 번 더 남긴다.
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        when(jdbc.queryForList(anyString(), any(Class.class), any()))
                .thenThrow(tableMissing())
                .thenReturn(List.of(80L))
                .thenThrow(tableMissing());
        ResponseDelayFilter filter = filterWith(jdbc);
        filter.refresh();
        assertEquals(1, filter.unavailableTransitions());
        filter.refresh();
        assertEquals(80L, filter.currentDelayMs());
        filter.refresh();
        assertEquals(2, filter.unavailableTransitions());
        assertEquals(0L, filter.currentDelayMs());
    }

    @Test
    void an_empty_control_row_means_no_delay() {
        ResponseDelayFilter filter = filterWith(jdbcReturning(null));
        filter.refresh();
        assertEquals(0L, filter.currentDelayMs());
    }

    @Test
    void an_interrupt_cancels_the_delay_but_never_reaches_the_chain() throws Exception {
        // 인터럽트 플래그를 복원한 채 체인에 들어가면 하류 JDBC·HTTP 가 즉시 실패해
        // 지연 주입이 실패 주입으로 바뀐다(slow-not-failed 붕괴).
        ResponseDelayFilter filter = filterWith(jdbcReturning(5_000L));
        filter.refresh();
        Thread.currentThread().interrupt();
        long elapsed = invoke(filter, "/api/transfers");
        assertFalse(Thread.currentThread().isInterrupted(),
                "the interrupt flag reached the filter chain");
        assertTrue(elapsed < 1_000L, "the interrupt did not cancel the remaining delay");
    }

    @Test
    void clamps_a_value_that_exceeds_the_ddl_ceiling() {
        // DDL 의 CHECK 를 우회한 행(수동 UPDATE, 이전 스키마)이 홉을 사실상 죽이지 못하게 한다.
        ResponseDelayFilter filter = filterWith(jdbcReturning(3_600_000L));
        filter.refresh();
        assertEquals(10_000L, filter.currentDelayMs());
    }
}
