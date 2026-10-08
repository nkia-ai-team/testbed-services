package com.fooddelivery.dispatch.service;

import com.fooddelivery.dispatch.repository.DispatchEventLogRepository;
import com.fooddelivery.dispatch.repository.DispatchRepository;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.InOrder;
import org.springframework.data.domain.PageRequest;

import java.time.LocalDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyCollection;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 배차 정리가 지켜야 할 계약: (1) 끝난 배차(DELIVERED)만, 보존 기간이 지난 것만 지운다 — ASSIGNED 는
 * 용량 게이트가 세는 진행 중 상태라 아무리 오래돼도 남는다. (2) 이력을 배차보다 먼저 지운다.
 * (3) 한 주기가 묶음 크기를 넘지 않고, 커서가 남겨 둔 행에 막히지 않는다. 나머지는 스위치와 실패 격리다.
 */
class DispatchRetentionTest {

    private final DispatchRepository dispatches = mock(DispatchRepository.class);
    private final DispatchEventLogRepository events = mock(DispatchEventLogRepository.class);
    private final DispatchRetentionService service = new DispatchRetentionService(dispatches, events);

    private DispatchRepository.RetentionHead head(long id, String status, LocalDateTime assignedAt) {
        DispatchRepository.RetentionHead head = mock(DispatchRepository.RetentionHead.class);
        when(head.getId()).thenReturn(id);
        when(head.getStatus()).thenReturn(status);
        when(head.getAssignedAt()).thenReturn(assignedAt);
        return head;
    }

    private void heads(DispatchRepository.RetentionHead... rows) {
        when(dispatches.findRetentionHeads(anyLong(), any())).thenReturn(List.of(rows));
    }

    @Test
    void only_delivered_dispatches_past_the_retention_period_are_deleted_with_their_events_first() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(1L, "DELIVERED", now.minusDays(45)),
                head(2L, "ASSIGNED", now.minusDays(45)),   // 진행 중 — 오래돼도 남긴다
                head(3L, "DELIVERED", now.minusDays(31)),
                head(4L, "DELIVERED", now.minusDays(29)));  // 보존 기간 안
        when(events.deleteByDispatchIdIn(anyCollection())).thenReturn(4);

        DispatchRetentionService.Result result = service.purgeOlderThan(30, 0L, 2000);

        assertEquals(2, result.purged());
        assertEquals(4, result.purgedEvents());
        InOrder order = inOrder(events, dispatches);
        order.verify(events).deleteByDispatchIdIn(List.of(1L, 3L));
        order.verify(dispatches).deleteAllByIdInBatch(List.of(1L, 3L));
    }

    @Test
    void the_cutoff_is_exactly_the_retention_period() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(1L, "DELIVERED", now.minusDays(30).minusMinutes(1)),
                head(2L, "DELIVERED", now.minusDays(30).plusMinutes(1)));

        service.purgeOlderThan(30, 0L, 2000);

        verify(dispatches).deleteAllByIdInBatch(List.of(1L));
    }

    @Test
    void no_delete_statement_is_issued_when_nothing_in_the_batch_is_finished_and_old() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(1L, "ASSIGNED", now.minusDays(40)), head(2L, "DELIVERED", now.minusDays(1)));

        assertEquals(0, service.purgeOlderThan(30, 0L, 2000).purged());

        verify(events, never()).deleteByDispatchIdIn(anyCollection());
        verify(dispatches, never()).deleteAllByIdInBatch(anyList());
    }

    @Test
    void one_batch_reads_no_more_than_the_batch_size_after_the_cursor() {
        heads();

        service.purgeOlderThan(30, 777L, 200);

        verify(dispatches).findRetentionHeads(eq(777L), eq(PageRequest.of(0, 200)));
    }

    @Test
    void the_cursor_moves_past_a_full_batch_of_old_rows_so_kept_rows_cannot_stall_the_purge() {
        LocalDateTime old = LocalDateTime.now().minusDays(40);
        // 묶음이 꽉 찼고 전부 진행 중(남길 행)이다. 다음 묶음은 그 뒤에서 이어 읽어야 한다.
        heads(head(10L, "ASSIGNED", old), head(11L, "ASSIGNED", old));

        assertEquals(11L, service.purgeOlderThan(30, 0L, 2).resumeAfterId());
    }

    @Test
    void the_cursor_returns_to_the_start_once_the_batch_reaches_rows_inside_the_retention_period() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(10L, "DELIVERED", now.minusDays(40)), head(11L, "DELIVERED", now.minusDays(2)));

        assertEquals(0L, service.purgeOlderThan(30, 5L, 2).resumeAfterId());
    }

    @Test
    void the_cursor_returns_to_the_start_at_the_end_of_the_table() {
        heads(head(10L, "DELIVERED", LocalDateTime.now().minusDays(40)));

        assertEquals(0L, service.purgeOlderThan(30, 5L, 2000).resumeAfterId());
    }

    @Test
    void the_batch_keeps_the_cursor_between_cycles() {
        LocalDateTime old = LocalDateTime.now().minusDays(40);
        // 목을 stubbing 도중에 만들면 Mockito 가 거부하므로 행을 먼저 만든다.
        List<DispatchRepository.RetentionHead> first = List.of(head(1L, "DELIVERED", old), head(2L, "DELIVERED", old));
        List<DispatchRepository.RetentionHead> second = List.of(head(3L, "DELIVERED", old));
        when(dispatches.findRetentionHeads(anyLong(), any())).thenReturn(first).thenReturn(second);
        DispatchRetentionBatch batch = new DispatchRetentionBatch(service, true, 30, 2);

        batch.purge();
        batch.purge();

        ArgumentCaptor<Long> after = ArgumentCaptor.forClass(Long.class);
        verify(dispatches, times(2)).findRetentionHeads(after.capture(), any());
        assertEquals(List.of(0L, 2L), after.getAllValues());
        assertEquals(0L, batch.resumeAfterId());
    }

    @Test
    void the_switch_stops_the_batch_before_it_touches_the_table() {
        new DispatchRetentionBatch(service, false, 30, 2000).purge();

        verify(dispatches, never()).findRetentionHeads(anyLong(), any());
    }

    @Test
    void a_failing_purge_does_not_escape_into_the_scheduler_and_keeps_the_cursor() {
        LocalDateTime old = LocalDateTime.now().minusDays(40);
        List<DispatchRepository.RetentionHead> kept = List.of(head(1L, "ASSIGNED", old), head(2L, "ASSIGNED", old));
        when(dispatches.findRetentionHeads(anyLong(), any()))
                .thenReturn(kept)
                .thenThrow(new IllegalStateException("Lock wait timeout exceeded"));
        DispatchRetentionBatch batch = new DispatchRetentionBatch(service, true, 30, 2);

        batch.purge();
        batch.purge();

        assertEquals(2L, batch.resumeAfterId());
    }
}
