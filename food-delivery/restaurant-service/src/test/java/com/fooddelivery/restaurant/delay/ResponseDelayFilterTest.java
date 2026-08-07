package com.fooddelivery.restaurant.delay;

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
 * F21-Q 지연 표면의 계약: (1) 행이 지시한 만큼만 늦춘다, (2) 제어 표면이 죽어도
 * restaurant 홉은 죽지 않는다, (3) 조회는 요청마다가 아니라 주기마다다.
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
        ResponseDelayFilter filter = new ResponseDelayFilter(repositoryReturning(120L), "restaurant", 2000L);
        assertTrue(invoke(filter) >= 100L, "control row asked for 120ms but the request was not delayed");
    }

    @Test
    void applies_no_delay_when_the_control_surface_is_unreachable() throws Exception {
        ResponseDelayControlRepository failing = mock(ResponseDelayControlRepository.class);
        when(failing.findById(anyString())).thenThrow(new IllegalStateException("db down"));
        assertTrue(invoke(new ResponseDelayFilter(failing, "restaurant", 2000L)) < 100L);

        ResponseDelayControlRepository empty = mock(ResponseDelayControlRepository.class);
        when(empty.findById(anyString())).thenReturn(Optional.empty());
        assertTrue(invoke(new ResponseDelayFilter(empty, "restaurant", 2000L)) < 100L);
    }

    @Test
    void reads_the_control_row_once_per_refresh_interval_not_once_per_request() throws Exception {
        ResponseDelayControlRepository repository = repositoryReturning(0L);
        ResponseDelayFilter filter = new ResponseDelayFilter(repository, "restaurant", 60_000L);
        for (int i = 0; i < 5; i++) {
            invoke(filter);
        }
        verify(repository, times(1)).findById("restaurant");
    }

    @Test
    void clamps_a_value_that_exceeds_the_ddl_ceiling() {
        ResponseDelayFilter filter = new ResponseDelayFilter(repositoryReturning(3_600_000L), "restaurant", 2000L);
        assertEquals(10_000L, filter.currentDelayMs());
    }
}
