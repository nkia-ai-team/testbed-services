package com.corebanking.common.outbox;

import org.junit.jupiter.api.Test;
import org.springframework.data.domain.PageRequest;

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
class OutboxPurgerTest {

    private final OutboxEventRepository events = mock(OutboxEventRepository.class);
    private final OutboxRelayStore store = new OutboxRelayStore(events, mock(OutboxRelayControlRepository.class));

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

        new OutboxPurger(store, true, 24, 500).purge();

        verify(events).deleteAllByIdInBatch(List.of(1L, 2L));
    }

    @Test
    void unpublished_events_are_never_deleted_however_old_they_are() {
        LocalDateTime now = LocalDateTime.now();
        // id 1 은 가장 오래됐지만 아직 발행되지 않았다(릴레이 정지·발행 실패). 남아야 한다.
        oldest(head(1L, null), head(2L, now.minusHours(48)));

        new OutboxPurger(store, true, 24, 500).purge();

        verify(events).deleteAllByIdInBatch(List.of(2L));
    }

    @Test
    void nothing_is_deleted_when_every_head_row_is_inside_the_retention_window() {
        LocalDateTime now = LocalDateTime.now();
        oldest(head(1L, now.minusHours(1)), head(2L, null));

        new OutboxPurger(store, true, 24, 500).purge();

        verify(events, never()).deleteAllByIdInBatch(anyList());
    }

    @Test
    void one_cycle_looks_at_no_more_than_the_batch_size() {
        oldest();

        new OutboxPurger(store, true, 24, 200).purge();

        verify(events).findOldest(PageRequest.of(0, 200));
    }

    @Test
    void the_switch_stops_the_purge_before_it_touches_the_table() {
        new OutboxPurger(store, false, 24, 500).purge();

        verify(events, never()).findOldest(any());
        verify(events, never()).deleteAllByIdInBatch(anyList());
    }

    @Test
    void a_failing_purge_does_not_escape_into_the_scheduler() {
        when(events.findOldest(any())).thenThrow(new IllegalStateException("ORA-12081: table is read only"));

        // 던지면 스케줄러가 매 주기 ERROR 스택을 찍는다. 삼키고 다음 주기에 다시 시도한다.
        new OutboxPurger(store, true, 24, 500).purge();
    }
}
