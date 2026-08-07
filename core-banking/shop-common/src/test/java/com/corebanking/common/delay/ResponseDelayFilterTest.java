package com.corebanking.common.delay;

import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * F21-P 지연 표면의 계약: (1) 행이 지시한 만큼만 늦춘다, (2) 제어 표면이 죽어도
 * 홉은 죽지 않는다, (3) 조회는 요청마다가 아니라 주기마다다.
 */
class ResponseDelayFilterTest {

    private static ResponseDelayControlRepository repositoryReturning(long delayMs) {
        ResponseDelayControl row = mock(ResponseDelayControl.class);
        when(row.getDelayMs()).thenReturn(delayMs);
        ResponseDelayControlRepository repository = mock(ResponseDelayControlRepository.class);
        when(repository.findById(anyString())).thenReturn(Optional.of(row));
        return repository;
    }

    private static long invoke(ResponseDelayFilter filter) throws Exception {
        MockFilterChain chain = new MockFilterChain();
        long started = System.nanoTime();
        filter.doFilterInternal(new MockHttpServletRequest(), new MockHttpServletResponse(), chain);
        long elapsedMs = (System.nanoTime() - started) / 1_000_000L;
        // 체인은 언제나 이어져야 한다 — 지연은 요청을 늦출 뿐 끊지 않는다(slow-not-failed).
        assertNotNull(chain.getRequest(), "filter chain was not continued");
        return elapsedMs;
    }

    @Test
    void delays_the_response_by_the_value_in_the_control_row() throws Exception {
        ResponseDelayFilter filter = new ResponseDelayFilter(repositoryReturning(120L), "transfer", 2000L);
        assertTrue(invoke(filter) >= 100L, "control row asked for 120ms but the request was not delayed");
    }

    @Test
    void applies_no_delay_when_the_control_surface_is_unreachable() throws Exception {
        // 제어 표면이 죽어도 앱은 정상이어야 한다. 조회 실패는 지연 근거가 아니다.
        ResponseDelayControlRepository failing = mock(ResponseDelayControlRepository.class);
        when(failing.findById(anyString())).thenThrow(new IllegalStateException("db down"));
        assertTrue(invoke(new ResponseDelayFilter(failing, "transfer", 2000L)) < 100L);

        // 행 부재도 같다 — 스위치보다 앞선 앱 빌드에서 홉이 제멋대로 느려지면 안 된다.
        ResponseDelayControlRepository empty = mock(ResponseDelayControlRepository.class);
        when(empty.findById(anyString())).thenReturn(Optional.empty());
        assertTrue(invoke(new ResponseDelayFilter(empty, "transfer", 2000L)) < 100L);
    }

    @Test
    void reads_the_control_row_once_per_refresh_interval_not_once_per_request() throws Exception {
        // 요청마다 조회하면 컨트롤 질의가 평시 노이즈와 구별되는 패턴으로 남는다.
        ResponseDelayControlRepository repository = repositoryReturning(0L);
        ResponseDelayFilter filter = new ResponseDelayFilter(repository, "transfer", 60_000L);
        for (int i = 0; i < 5; i++) {
            invoke(filter);
        }
        verify(repository, times(1)).findById("transfer");
    }

    @Test
    void clamps_a_value_that_exceeds_the_ddl_ceiling() {
        // DDL 의 CHECK 를 우회한 행(수동 UPDATE, 이전 스키마)이 홉을 사실상 죽이지 못하게 한다.
        ResponseDelayFilter filter = new ResponseDelayFilter(repositoryReturning(3_600_000L), "transfer", 2000L);
        assertEquals(10_000L, filter.currentDelayMs());
    }
}
