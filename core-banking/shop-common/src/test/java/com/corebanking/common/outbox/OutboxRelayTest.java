package com.corebanking.common.outbox;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.SendResult;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.EnableTransactionManagement;
import org.springframework.transaction.support.AbstractPlatformTransactionManager;
import org.springframework.transaction.support.DefaultTransactionStatus;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 릴레이의 결함은 "Kafka 를 기다리는 동안 DB 커넥션을 쥐고 있다" 였다. 그 결함은 단위
 * 동작으로는 안 보이고 구조로만 보이므로, 여기서는 실제 트랜잭션 프록시를 띄워
 * 발행 시점에 트랜잭션이 열려 있는지를 직접 관측한다. 나머지 테스트는 at-least-once
 * 계약(성공분만 마킹, 실패·미완료분은 published_at 미기록)을 고정한다.
 */
class OutboxRelayTest {

    private AnnotationConfigApplicationContext context;

    @AfterEach
    void closeContext() {
        if (context != null) {
            context.close();
        }
    }

    /** 실제 트랜잭션 경계만 만드는 매니저. 커밋·롤백은 할 일이 없고 활성 여부만 진짜다. */
    static class NoopTransactionManager extends AbstractPlatformTransactionManager {
        @Override
        protected Object doGetTransaction() {
            return new Object();
        }

        @Override
        protected void doBegin(Object transaction, TransactionDefinition definition) {
        }

        @Override
        protected void doCommit(DefaultTransactionStatus status) {
        }

        @Override
        protected void doRollback(DefaultTransactionStatus status) {
        }
    }

    @Configuration
    @EnableTransactionManagement
    static class TestConfig {

        @Bean
        PlatformTransactionManager transactionManager() {
            return new NoopTransactionManager();
        }

        @Bean
        OutboxEventRepository outboxEventRepository() {
            return mock(OutboxEventRepository.class);
        }

        @Bean
        OutboxRelayControlRepository controlRepository() {
            return mock(OutboxRelayControlRepository.class);
        }

        @Bean
        @SuppressWarnings("unchecked")
        KafkaTemplate<String, String> kafkaTemplate() {
            return mock(KafkaTemplate.class);
        }

        @Bean
        OutboxRelayStore outboxRelayStore(OutboxEventRepository events, OutboxRelayControlRepository control) {
            return new OutboxRelayStore(events, control);
        }

        @Bean
        OutboxRelay outboxRelay(OutboxRelayStore store, KafkaTemplate<String, String> kafkaTemplate) {
            return new OutboxRelay(store, kafkaTemplate, "transfer", 300L);
        }
    }

    private OutboxEvent event(long id, String topic) {
        OutboxEvent event = new OutboxEvent();
        event.setId(id);
        event.setTopic(topic);
        event.setAggregateId("agg-" + id);
        event.setPayload("{\"id\":" + id + "}");
        return event;
    }

    private void start() {
        context = new AnnotationConfigApplicationContext();
        context.register(TestConfig.class);
        context.refresh();
    }

    private OutboxEventRepository events() {
        return context.getBean(OutboxEventRepository.class);
    }

    @SuppressWarnings("unchecked")
    private KafkaTemplate<String, String> kafka() {
        return context.getBean(KafkaTemplate.class);
    }

    private void pending(OutboxEvent... rows) {
        when(events().findTop100ByPublishedAtIsNullOrderByCreatedAtAsc()).thenReturn(List.of(rows));
        when(context.getBean(OutboxRelayControlRepository.class).findById(anyString()))
                .thenReturn(Optional.empty());
    }

    @Test
    void kafka_is_never_waited_on_while_a_transaction_is_open() {
        start();
        pending(event(1L, "banking.transfers"), event(2L, "banking.transfers"));

        List<Boolean> transactionActiveAtSend = new CopyOnWriteArrayList<>();
        when(kafka().send(anyString(), anyString(), anyString())).thenAnswer(invocation -> {
            transactionActiveAtSend.add(TransactionSynchronizationManager.isActualTransactionActive());
            return CompletableFuture.completedFuture(mock(SendResult.class));
        });

        context.getBean(OutboxRelay.class).relay();

        assertEquals(2, transactionActiveAtSend.size(), "the relay did not publish the pending batch");
        assertFalse(transactionActiveAtSend.contains(true),
                "Kafka 발행이 트랜잭션 안에서 일어났다 — 브로커를 기다리는 동안 Hikari 커넥션을 쥔다");
    }

    @Test
    void relay_carries_no_transactional_annotation() throws NoSuchMethodException {
        // 위 테스트는 프록시가 걸린 뒤의 동작을 본다. 이 테스트는 원인 쪽을 직접 고정한다 —
        // relay() 에 @Transactional 이 다시 붙는 것이 되살아나는 결함의 정확한 형태다.
        assertFalse(OutboxRelay.class.getDeclaredMethod("relay")
                        .isAnnotationPresent(org.springframework.transaction.annotation.Transactional.class),
                "relay() 가 다시 @Transactional 이 됐다 — 발행 구간이 트랜잭션 안으로 들어온다");
        assertFalse(OutboxRelay.class.isAnnotationPresent(
                        org.springframework.transaction.annotation.Transactional.class),
                "OutboxRelay 클래스에 @Transactional 이 붙었다 — relay() 전체가 트랜잭션이 된다");
    }

    @Test
    @SuppressWarnings("unchecked")
    void only_the_events_that_actually_published_are_marked() {
        start();
        pending(event(1L, "banking.transfers"), event(2L, "banking.transfers"), event(3L, "banking.transfers"));

        CompletableFuture<SendResult<String, String>> failed = new CompletableFuture<>();
        failed.completeExceptionally(new IllegalStateException("broker refused"));
        when(kafka().send(anyString(), eq("agg-1"), anyString()))
                .thenReturn(CompletableFuture.completedFuture(mock(SendResult.class)));
        when(kafka().send(anyString(), eq("agg-2"), anyString())).thenReturn(failed);
        when(kafka().send(anyString(), eq("agg-3"), anyString()))
                .thenReturn(CompletableFuture.completedFuture(mock(SendResult.class)));

        context.getBean(OutboxRelay.class).relay();

        verify(events()).markPublished(eq(List.of(1L, 3L)), any(LocalDateTime.class));
        // 건당 save 가 되살아나면 커넥션 점유가 다시 배치 크기만큼 늘어난다.
        verify(events(), never()).save(any());
    }

    @Test
    void nothing_is_marked_when_the_whole_batch_fails_to_publish() {
        start();
        pending(event(1L, "banking.transfers"), event(2L, "banking.transfers"));

        when(kafka().send(anyString(), anyString(), anyString()))
                .thenThrow(new IllegalStateException("producer buffer exhausted"));

        context.getBean(OutboxRelay.class).relay();

        // published_at 이 null 로 남아야 다음 주기가 재시도한다(at-least-once).
        verify(events(), never()).markPublished(anyList(), any(LocalDateTime.class));
    }

    @Test
    @SuppressWarnings("unchecked")
    void a_broker_that_never_answers_bounds_the_cycle_and_marks_nothing() {
        start();
        pending(event(1L, "banking.transfers"), event(2L, "banking.transfers"));

        // 영영 완료되지 않는 발행. 예전 구조라면 여기서 delivery.timeout(기본 120s) 까지
        // 커넥션을 쥔 채 멎었다.
        when(kafka().send(anyString(), eq("agg-1"), anyString())).thenReturn(new CompletableFuture<>());
        when(kafka().send(anyString(), eq("agg-2"), anyString()))
                .thenReturn(CompletableFuture.completedFuture(mock(SendResult.class)));

        long startedAt = System.currentTimeMillis();
        context.getBean(OutboxRelay.class).relay();
        long elapsed = System.currentTimeMillis() - startedAt;

        assertTrue(elapsed < 3_000L, "발행 대기가 유계가 아니다 — 한 주기가 " + elapsed + "ms 걸렸다");
        verify(events()).markPublished(eq(List.of(2L)), any(LocalDateTime.class));
    }

    @Test
    void the_control_row_stops_the_relay_before_it_reads_the_outbox() {
        start();
        OutboxRelayControl stopped = mock(OutboxRelayControl.class);
        when(stopped.isEnabled()).thenReturn(false);
        when(context.getBean(OutboxRelayControlRepository.class).findById("transfer"))
                .thenReturn(Optional.of(stopped));

        context.getBean(OutboxRelay.class).relay();

        verify(events(), never()).findTop100ByPublishedAtIsNullOrderByCreatedAtAsc();
        verify(kafka(), never()).send(anyString(), anyString(), anyString());
    }

    @Test
    @SuppressWarnings("unchecked")
    void a_failing_control_lookup_does_not_stop_the_relay() {
        start();
        when(context.getBean(OutboxRelayControlRepository.class).findById("transfer"))
                .thenThrow(new IllegalStateException("control table missing"));
        when(events().findTop100ByPublishedAtIsNullOrderByCreatedAtAsc())
                .thenReturn(List.of(event(1L, "banking.transfers")));
        when(kafka().send(anyString(), anyString(), anyString()))
                .thenReturn(CompletableFuture.completedFuture(mock(SendResult.class)));

        context.getBean(OutboxRelay.class).relay();

        // fail-open: 스위치를 못 읽는 것은 정지 근거가 아니다.
        verify(events()).markPublished(eq(List.of(1L)), any(LocalDateTime.class));
    }
}
