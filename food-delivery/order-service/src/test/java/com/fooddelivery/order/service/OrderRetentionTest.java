package com.fooddelivery.order.service;

import com.fooddelivery.order.repository.OrderItemRepository;
import com.fooddelivery.order.repository.OrderRepository;
import org.junit.jupiter.api.Test;
import org.mockito.InOrder;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.Slice;

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
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 주문 정리가 지켜야 할 계약: (1) 끝난 주문(DELIVERED, CANCELLED)만, 보존 기간이 지난 것만 지운다 —
 * PENDING 은 진행 중이라 남는다. (2) 주문 항목을 주문보다 먼저 지운다(fk_order_items_order 에
 * ON DELETE 가 없다). (3) 한 주기가 묶음 크기를 넘지 않고 커서가 남겨 둔 행에 막히지 않는다.
 */
class OrderRetentionTest {

    private final OrderRepository orders = mock(OrderRepository.class);
    private final OrderItemRepository items = mock(OrderItemRepository.class);
    private final OrderRetentionService service = new OrderRetentionService(orders, items);

    private OrderRepository.RetentionHead head(long id, String status, LocalDateTime createdAt) {
        OrderRepository.RetentionHead head = mock(OrderRepository.RetentionHead.class);
        when(head.getId()).thenReturn(id);
        when(head.getStatus()).thenReturn(status);
        when(head.getCreatedAt()).thenReturn(createdAt);
        return head;
    }

    private void heads(OrderRepository.RetentionHead... rows) {
        when(orders.findRetentionHeads(anyLong(), any())).thenReturn(List.of(rows));
    }

    @Test
    void only_finished_orders_past_the_retention_period_are_deleted_with_their_items_first() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(1L, "DELIVERED", now.minusDays(60)),
                head(2L, "PENDING", now.minusDays(60)),     // 진행 중 — 묵은 것은 정리 배치가 먼저 취소한다
                head(3L, "CANCELLED", now.minusDays(31)),
                head(4L, "CANCELLED", now.minusDays(29)));  // 보존 기간 안
        when(items.deleteByOrderIdIn(anyCollection())).thenReturn(5);

        OrderRetentionService.Result result = service.purgeOlderThan(30, 0L, 2000);

        assertEquals(2, result.purged());
        assertEquals(5, result.purgedItems());
        InOrder order = inOrder(items, orders);
        order.verify(items).deleteByOrderIdIn(List.of(1L, 3L));
        order.verify(orders).deleteAllByIdInBatch(List.of(1L, 3L));
    }

    @Test
    void the_cutoff_is_exactly_the_retention_period() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(1L, "CANCELLED", now.minusDays(30).minusMinutes(1)),
                head(2L, "CANCELLED", now.minusDays(30).plusMinutes(1)));

        service.purgeOlderThan(30, 0L, 2000);

        verify(orders).deleteAllByIdInBatch(List.of(1L));
    }

    @Test
    void no_delete_statement_is_issued_when_nothing_in_the_batch_is_finished_and_old() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(1L, "PENDING", now.minusDays(40)), head(2L, "DELIVERED", now.minusDays(1)));

        assertEquals(0, service.purgeOlderThan(30, 0L, 2000).purged());

        verify(items, never()).deleteByOrderIdIn(anyCollection());
        verify(orders, never()).deleteAllByIdInBatch(anyList());
    }

    @Test
    void one_batch_reads_no_more_than_the_batch_size_after_the_cursor() {
        heads();

        assertEquals(0L, service.purgeOlderThan(30, 99L, 500).resumeAfterId());

        verify(orders).findRetentionHeads(eq(99L), eq(PageRequest.of(0, 500)));
    }

    @Test
    void the_cursor_moves_past_kept_rows_and_returns_to_the_start_at_recent_rows() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(7L, "PENDING", now.minusDays(40)), head(8L, "PENDING", now.minusDays(40)));
        assertEquals(8L, service.purgeOlderThan(30, 0L, 2).resumeAfterId());

        heads(head(9L, "CANCELLED", now.minusDays(40)), head(10L, "PENDING", now.minusHours(1)));
        assertEquals(0L, service.purgeOlderThan(30, 8L, 2).resumeAfterId());
    }

    @Test
    void the_switch_stops_the_batch_before_it_touches_the_table() {
        new OrderRetentionBatch(service, false, 30, 2000).purge();

        verify(orders, never()).findRetentionHeads(anyLong(), any());
    }

    @Test
    void a_failing_purge_does_not_escape_into_the_scheduler() {
        when(orders.findRetentionHeads(anyLong(), any())).thenThrow(new IllegalStateException("Lock wait timeout exceeded"));

        OrderRetentionBatch batch = new OrderRetentionBatch(service, true, 30, 2000);
        batch.purge();

        assertEquals(0L, batch.resumeAfterId());
    }

    @Test
    void the_order_list_query_returns_a_slice_so_spring_data_issues_no_count_query() throws Exception {
        // GET /api/orders?page=0&size=10 (F21-Q 의 「무관 읽기」)가 페이지마다 주문 전체를 세지 않게 한다.
        assertEquals(Slice.class, OrderRepository.class.getMethod("search", String.class, String.class,
                Long.class, LocalDateTime.class, LocalDateTime.class, Pageable.class).getReturnType());
    }
}
