package com.fooddelivery.restaurant.delay;

import org.junit.jupiter.api.Test;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.fail;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * 나머지 단위테스트는 필터를 생성자로 직접 만들고 refresh() 를 손으로 부른다. 그래서
 * {@code @PostConstruct} 가 통째로 사라져도 전부 green 이고, 운영에서는 delayMs 가
 * 영원히 0 인 채 — 주입 표면이 조용히 죽은 채 — 배포된다. 여기서는 실제 스프링
 * 라이프사이클로 띄워 폴러가 스스로 도는지, 그리고 종료 시 멎는지를 고정한다.
 */
class ResponseDelayFilterSpringLifecycleTest {

    private static final AtomicLong ROW = new AtomicLong(0L);
    private static final AtomicInteger QUERIES = new AtomicInteger();
    private static final AtomicReference<String> POLLER_THREAD = new AtomicReference<>();

    @Configuration
    static class TestConfig {

        @Bean
        @SuppressWarnings("unchecked")
        JdbcTemplate jdbcTemplate() {
            JdbcTemplate jdbc = mock(JdbcTemplate.class);
            when(jdbc.queryForList(anyString(), any(Class.class), any())).thenAnswer(invocation -> {
                POLLER_THREAD.set(Thread.currentThread().getName());
                QUERIES.incrementAndGet();
                return List.of(ROW.get());
            });
            return jdbc;
        }

        @Bean
        ResponseDelayFilter responseDelayFilter(JdbcTemplate jdbcTemplate) {
            return new ResponseDelayFilter(jdbcTemplate, "restaurant", 20L);
        }
    }

    private static void awaitUntil(BooleanSupplier condition, String message) throws InterruptedException {
        long deadline = System.currentTimeMillis() + 3_000L;
        while (System.currentTimeMillis() < deadline) {
            if (condition.getAsBoolean()) {
                return;
            }
            Thread.sleep(10L);
        }
        fail(message);
    }

    @Test
    void the_poller_starts_itself_and_keeps_tracking_the_row_without_any_manual_refresh()
            throws InterruptedException {
        ROW.set(700L);
        QUERIES.set(0);
        POLLER_THREAD.set(null);

        AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext();
        context.register(TestConfig.class);
        context.refresh();
        ResponseDelayFilter filter = context.getBean(ResponseDelayFilter.class);

        // @PostConstruct 가 폴러를 띄운다. 아무도 refresh() 를 부르지 않는다.
        awaitUntil(() -> filter.currentDelayMs() == 700L,
                "the poller never started: delayMs stayed at " + filter.currentDelayMs());

        // 한 번 읽고 마는 것이 아니라 주기마다 따라간다 — 주입은 앱이 뜬 뒤에 일어난다.
        ROW.set(1_500L);
        awaitUntil(() -> filter.currentDelayMs() == 1_500L,
                "the poller stopped after the first read: delayMs stayed at " + filter.currentDelayMs());

        // 스레드 덤프·로그 패턴 수집에 주입 수단이 드러나면 안 된다(JDK 기본 이름이어야 한다).
        String threadName = POLLER_THREAD.get();
        assertNotNull(threadName);
        for (String confession : new String[]{"delay", "control", "response"}) {
            assertFalse(threadName.toLowerCase().contains(confession),
                    "poller thread name confesses the injection surface: " + threadName);
        }

        // @PreDestroy 가 폴러를 멎게 한다 — 컨텍스트가 닫혀도 도는 데몬은 다음 테스트·
        // 종료 절차에 남는다.
        context.close();
        int afterClose = QUERIES.get();
        Thread.sleep(200L);
        assertEquals(afterClose, QUERIES.get(), "the poller kept running after the context closed");
    }
}
