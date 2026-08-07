package com.corebanking.common.delay;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.concurrent.atomic.AtomicLong;

/**
 * response_delay_control 행이 지시하는 만큼 자기 서비스의 응답을 늦춘다.
 * 상류(api)는 느려진 하류(transfer)를 기다리다 Tomcat 스레드를 쌓는다 —
 * 대상은 죽지 않는다(slow-not-failed).
 *
 * <p>제어 값은 {@code response.delay.refresh-interval-ms}(기본 2초, OutboxRelay
 * 폴링과 같은 주기)마다 한 번만 읽어 캐시한다. 요청마다 조회하면 지연 자체보다
 * 컨트롤 조회가 DB 부하를 지배하고, 평시 노이즈와 구별되는 질의 패턴이 남는다.
 *
 * <p>조회 실패·행 부재는 지연 0 으로 폴백한다 — 제어 표면이 죽어도 앱은 정상이어야
 * 한다. 값은 DDL 의 CHECK 와 같은 범위(0~10000ms)로 앱에서도 한 번 더 조인다.
 */
@Component
@ConditionalOnProperty(name = "response.delay.service-id")
public class ResponseDelayFilter extends OncePerRequestFilter {

    private static final Logger log = LoggerFactory.getLogger(ResponseDelayFilter.class);
    private static final long MAX_DELAY_MS = 10_000L;

    private final ResponseDelayControlRepository controlRepository;
    private final String serviceId;
    private final long refreshIntervalMs;
    private final AtomicLong nextRefreshAt = new AtomicLong(Long.MIN_VALUE);
    private volatile long delayMs = 0L;

    public ResponseDelayFilter(ResponseDelayControlRepository controlRepository,
                               @Value("${response.delay.service-id}") String serviceId,
                               @Value("${response.delay.refresh-interval-ms:2000}") long refreshIntervalMs) {
        this.controlRepository = controlRepository;
        this.serviceId = serviceId;
        this.refreshIntervalMs = refreshIntervalMs;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        long delay = currentDelayMs();
        if (delay > 0) {
            try {
                Thread.sleep(delay);
            } catch (InterruptedException ex) {
                Thread.currentThread().interrupt();
            }
        }
        chain.doFilter(request, response);
    }

    long currentDelayMs() {
        long now = System.currentTimeMillis();
        long due = nextRefreshAt.get();
        // 만료된 순간 첫 요청 하나만 조회한다. CAS 에서 진 스레드는 직전 값을 그대로 쓴다.
        if (now >= due && nextRefreshAt.compareAndSet(due, now + refreshIntervalMs)) {
            delayMs = readControl();
        }
        return delayMs;
    }

    private long readControl() {
        try {
            return controlRepository.findById(serviceId)
                    .map(ResponseDelayControl::getDelayMs)
                    .map(value -> Math.max(0L, Math.min(MAX_DELAY_MS, value)))
                    .orElse(0L);
        } catch (Exception ex) {
            log.warn("Response delay control lookup failed (no delay applied): {}", ex.getMessage());
            return 0L;
        }
    }
}
