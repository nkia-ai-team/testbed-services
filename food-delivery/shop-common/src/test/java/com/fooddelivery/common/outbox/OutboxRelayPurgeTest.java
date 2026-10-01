package com.fooddelivery.common.outbox;

import org.junit.jupiter.api.Test;
import org.springframework.data.domain.PageRequest;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.test.util.ReflectionTestUtils;

import java.time.LocalDateTime;
import java.util.List;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 정리가 지켜야 할 계약은 둘이다. (1) 미발행 행은 어떤 경우에도 지우지 않는다 — 지우면
 * 이벤트가 유실되고 릴레이 정지 시나리오의 적체가 사라진다. (2) 보존 기간 안의 발행분은
 * 남긴다. 나머지는 스위치와 실패 격리다.
 */
class OutboxRelayPurgeTest {

    static class TestEvent extends OutboxEvent {
    }

    static class TestRelay extends OutboxRelay<TestEvent> {
        TestRelay(OutboxEventRepository<TestEvent> repository, KafkaTemplate<String, String> kafkaTemplate) {
            super(repository, kafkaTemplate);
        }
    }

    @SuppressWarnings("unchecked")
    private final OutboxEventRepository<TestEvent> events = mock(OutboxEventRepository.class);

    // 운영에서는 @Value 가 채우는 필드다. 기본값(켜짐, 24시간, 500행)을 그대로 넣는다.
    @SuppressWarnings("unchecked")
    private TestRelay relay(boolean enabled, int batchSize) {
        TestRelay relay = new TestRelay(events, mock(KafkaTemplate.class));
        ReflectionTestUtils.setField(relay, "purgeEnabled", enabled);
        ReflectionTestUtils.setField(relay, "purgeRetentionHours", 24L);
        ReflectionTestUtils.setField(relay, "purgeBatchSize", batchSize);
        return relay;
    }

    private OutboxEventRepository.Head head(long id, LocalDateTime publishedAt) {
        OutboxEventRepository.Head head = mock(OutboxEventRepository.Head.class);
        when(head.getId()).thenReturn(id);
        when(head.getPublishedAt()).thenReturn(publishedAt);
        return head;
    }

    private void oldest(OutboxEventRepository.Head... rows) {
        when(events.findOldest(any())).thenReturn(List.of(rows));
    }

    @Test
    void only_events_published_before_the_retention_window_are_deleted() {
        LocalDateTime now = LocalDateTime.now();
        oldest(head(1L, now.minusHours(30)), head(2L, now.minusHours(25)), head(3L, now.minusHours(2)));

        relay(true, 500).purgePublished();

        verify(events).deleteAllByIdInBatch(List.of(1L, 2L));
    }

    @Test
    void unpublished_events_are_never_deleted_however_old_they_are() {
        LocalDateTime now = LocalDateTime.now();
        // id 1 은 가장 오래됐지만 아직 발행되지 않았다(릴레이 정지·발행 실패). 남아야 한다.
        oldest(head(1L, null), head(2L, now.minusHours(48)));

        relay(true, 500).purgePublished();

        verify(events).deleteAllByIdInBatch(List.of(2L));
    }

    @Test
    void nothing_is_deleted_when_every_head_row_is_inside_the_retention_window() {
        LocalDateTime now = LocalDateTime.now();
        oldest(head(1L, now.minusHours(1)), head(2L, null));

        relay(true, 500).purgePublished();

        verify(events, never()).deleteAllByIdInBatch(anyList());
    }

    @Test
    void one_cycle_looks_at_no_more_than_the_batch_size() {
        oldest();

        relay(true, 200).purgePublished();

        verify(events).findOldest(PageRequest.of(0, 200));
    }

    @Test
    void the_switch_stops_the_purge_before_it_touches_the_table() {
        relay(false, 500).purgePublished();

        verify(events, never()).findOldest(any());
        verify(events, never()).deleteAllByIdInBatch(anyList());
    }

    @Test
    void a_failing_purge_does_not_escape_into_the_scheduler() {
        when(events.findOldest(any())).thenThrow(new IllegalStateException("Lock wait timeout exceeded"));

        // 던지면 스케줄러가 매 주기 ERROR 스택을 찍는다. 삼키고 다음 주기에 다시 시도한다.
        relay(true, 500).purgePublished();
    }
}
