package com.fooddelivery.restaurant.delay;

import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * response_delay_control 행이 지시하는 만큼 restaurant 응답을 늦춘다. 상류(order)는
 * 느려진 restaurant 를 기다리다 Tomcat 스레드를 쌓고, restaurant 는 죽지 않는다
 * (slow-not-failed). core-banking 의 같은 이름 필터와 동일한 계약이다.
 *
 * <p>제어 값은 <b>요청 스레드가 아니라</b> 데몬 폴러가 읽는다 — 요청 스레드에서 읽으면
 * otel 이 조회를 서버 스팬의 자식 JDBC CLIENT 스팬으로 붙여 "SELECT ...
 * response_delay_control 직후 같은 길이의 공백"을 트레이스에 자백한다.
 *
 * <p>조회는 JPA 가 아니라 {@link JdbcTemplate} 다. 엔티티로 매핑하면 테이블이 없는
 * 테스트베드에서 Hibernate 가 매 주기 ERROR 스택을 남기고 그게 OTLP 로 방송된다.
 * 실패 로그도 상태가 바뀔 때 한 번만 남긴다.
 *
 * <p>액추에이터 경로는 늦추지 않는다 — 프로브 timeoutSeconds 가 3 이라 delay_ms>=3000
 * 이면 지연이 곧 파드 재기동이 되고, 그 순간 지연 주입이 아니라 파드 장애 주입이 된다.
 */
@Component
@ConditionalOnProperty(name = "response.delay.service-id")
public class ResponseDelayFilter extends OncePerRequestFilter {

    private static final Logger log = LoggerFactory.getLogger(ResponseDelayFilter.class);
    private static final long MAX_DELAY_MS = 10_000L;
    private static final String SELECT_DELAY =
            "select delay_ms from response_delay_control where service_id = ?";
    /** 프로브·스크레이프 경로. 늦추면 지연 주입이 파드 장애 주입으로 바뀐다. */
    private static final String ACTUATOR_PREFIX = "/actuator";

    private final JdbcTemplate jdbcTemplate;
    private final String serviceId;
    private final long refreshIntervalMs;
    private volatile long delayMs = 0L;
    private volatile boolean controlUnavailable = false;
    /** 상태 변화마다 1 씩 는다 — "2초마다 같은 경고" 가 아님을 테스트가 고정한다. */
    private volatile int unavailableTransitions = 0;
    private ScheduledExecutorService poller;

    public ResponseDelayFilter(JdbcTemplate jdbcTemplate,
                               @Value("${response.delay.service-id}") String serviceId,
                               @Value("${response.delay.refresh-interval-ms:2000}") long refreshIntervalMs) {
        this.jdbcTemplate = jdbcTemplate;
        this.serviceId = serviceId;
        this.refreshIntervalMs = refreshIntervalMs;
    }

    @PostConstruct
    void startPolling() {
        poller = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "response-delay-control-poller");
            thread.setDaemon(true);
            return thread;
        });
        poller.scheduleWithFixedDelay(this::refresh, 0, refreshIntervalMs, TimeUnit.MILLISECONDS);
    }

    @PreDestroy
    void stopPolling() {
        if (poller != null) {
            poller.shutdownNow();
        }
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        return request.getRequestURI().startsWith(ACTUATOR_PREFIX);
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        long delay = delayMs;
        if (delay > 0) {
            try {
                Thread.sleep(delay);
            } catch (InterruptedException ex) {
                // 인터럽트 플래그를 복원하지 않는다. 복원한 채 체인에 들어가면 하류
                // JDBC·HTTP 클라이언트가 인터럽트를 보고 즉시 실패하고, 그러면 이 표면은
                // 지연 주입이 아니라 실패 주입이 된다 — slow-not-failed 가 무너진다.
                // 인터럽트가 취소하는 것은 남은 지연뿐이고 요청은 정상 처리한다.
            }
        }
        chain.doFilter(request, response);
    }

    /**
     * 폴러가 부르는 갱신. 예외를 밖으로 내보내면 안 된다 —
     * scheduleWithFixedDelay 는 한 번 던진 작업을 조용히 취소하고,
     * 그 뒤로는 지연이 영원히 갱신되지 않는다.
     */
    void refresh() {
        try {
            List<Long> rows = jdbcTemplate.queryForList(SELECT_DELAY, Long.class, serviceId);
            long value = rows.isEmpty() || rows.get(0) == null ? 0L : rows.get(0);
            delayMs = Math.max(0L, Math.min(MAX_DELAY_MS, value));
            controlUnavailable = false;
        } catch (Exception ex) {
            // 제어 표면이 죽어도 앱은 정상이어야 한다. 조회 실패는 지연 근거가 아니다.
            delayMs = 0L;
            if (!controlUnavailable) {
                controlUnavailable = true;
                unavailableTransitions++;
                log.warn("Response delay control unavailable, no delay applied: {}", ex.getMessage());
            }
        }
    }

    long currentDelayMs() {
        return delayMs;
    }

    int unavailableTransitions() {
        return unavailableTransitions;
    }
}
