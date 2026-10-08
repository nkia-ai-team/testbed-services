package com.fooddelivery.payment.service;

import com.fooddelivery.payment.repository.PaymentRepository;
import org.junit.jupiter.api.Test;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.Slice;

import java.time.LocalDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 결제 정리가 지켜야 할 계약: 끝난 결제만, 끝난 지 보존 기간이 지난 것만 지운다. 끝났다는 것은
 * FAILED(처리 시각 기준)이거나 정산 대상 상태이면서 정산된 것(정산 시각 기준)이다. PENDING, 아직
 * 정산되지 않은 승인 결제, 코드가 모르는 상태는 아무리 오래돼도 남는다 — 정산 배치가 읽는 행이다.
 */
class PaymentRetentionTest {

    private final PaymentRepository payments = mock(PaymentRepository.class);
    private final PaymentRetentionService service = new PaymentRetentionService(payments);

    private PaymentRepository.RetentionHead head(long id, String status, LocalDateTime createdAt,
                                                  LocalDateTime processedAt, LocalDateTime settledAt) {
        PaymentRepository.RetentionHead head = mock(PaymentRepository.RetentionHead.class);
        when(head.getId()).thenReturn(id);
        when(head.getStatus()).thenReturn(status);
        when(head.getCreatedAt()).thenReturn(createdAt);
        when(head.getProcessedAt()).thenReturn(processedAt);
        when(head.getSettledAt()).thenReturn(settledAt);
        return head;
    }

    private void heads(PaymentRepository.RetentionHead... rows) {
        when(payments.findRetentionHeads(anyLong(), any())).thenReturn(List.of(rows));
    }

    @Test
    void only_finished_payments_past_the_retention_period_are_deleted() {
        LocalDateTime d40 = LocalDateTime.now().minusDays(40);
        heads(head(1L, "FAILED", d40, d40, null),
                head(2L, "APPROVED", d40, d40, d40.plusHours(1)),   // 정산 완료
                head(3L, "APPROVED", d40, d40, null),               // 아직 정산 전 — 진행 중
                head(4L, "PENDING", d40, null, null),               // 진행 중
                head(5L, "TRANSIENT", d40, d40, null),              // 모르는 상태 — 끝났다고 가정하지 않는다
                head(6L, "COMPLETED", d40, d40, d40.plusHours(1)),
                head(7L, "SUCCESS", d40, d40, d40.plusHours(1)));

        assertEquals(4, service.purgeOlderThan(30, 0L, 2000).purged());

        verify(payments).deleteAllByIdInBatch(List.of(1L, 2L, 6L, 7L));
    }

    @Test
    void a_settled_payment_counts_from_its_settlement_time_not_its_creation_time() {
        LocalDateTime now = LocalDateTime.now();
        // 31일 전에 생겼지만 29일 전에 정산됐다. 정산이 끝난 지 30일이 안 됐으니 남는다.
        heads(head(1L, "APPROVED", now.minusDays(31), now.minusDays(31), now.minusDays(29)),
                head(2L, "APPROVED", now.minusDays(32), now.minusDays(32), now.minusDays(31)));

        service.purgeOlderThan(30, 0L, 2000);

        verify(payments).deleteAllByIdInBatch(List.of(2L));
    }

    @Test
    void a_failed_payment_without_a_processing_time_falls_back_to_its_creation_time() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(1L, "FAILED", now.minusDays(31), null, null), head(2L, "FAILED", now.minusDays(29), null, null));

        service.purgeOlderThan(30, 0L, 2000);

        verify(payments).deleteAllByIdInBatch(List.of(1L));
    }

    @Test
    void no_delete_statement_is_issued_when_nothing_in_the_batch_is_finished_and_old() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(1L, "PENDING", now.minusDays(40), null, null), head(2L, "FAILED", now.minusDays(1), now, null));

        assertEquals(0, service.purgeOlderThan(30, 0L, 2000).purged());

        verify(payments, never()).deleteAllByIdInBatch(anyList());
    }

    @Test
    void one_batch_reads_no_more_than_the_batch_size_after_the_cursor_and_the_cursor_advances() {
        LocalDateTime d40 = LocalDateTime.now().minusDays(40);
        heads(head(41L, "PENDING", d40, null, null), head(42L, "APPROVED", d40, d40, null));

        assertEquals(42L, service.purgeOlderThan(30, 40L, 2).resumeAfterId());

        verify(payments).findRetentionHeads(eq(40L), eq(PageRequest.of(0, 2)));
    }

    @Test
    void the_cursor_returns_to_the_start_once_the_batch_reaches_recently_created_rows() {
        LocalDateTime now = LocalDateTime.now();
        heads(head(41L, "FAILED", now.minusDays(40), now.minusDays(40), null),
                head(42L, "PENDING", now.minusMinutes(5), null, null));

        assertEquals(0L, service.purgeOlderThan(30, 40L, 2).resumeAfterId());
    }

    @Test
    void the_switch_stops_the_batch_before_it_touches_the_table() {
        new PaymentRetentionBatch(service, false, 30, 2000).purge();

        verify(payments, never()).findRetentionHeads(anyLong(), any());
    }

    @Test
    void a_failing_purge_does_not_escape_into_the_scheduler() {
        when(payments.findRetentionHeads(anyLong(), any())).thenThrow(new IllegalStateException("Lock wait timeout exceeded"));

        new PaymentRetentionBatch(service, true, 30, 2000).purge();
    }

    @Test
    void the_payment_list_query_returns_a_slice_so_spring_data_issues_no_count_query() throws Exception {
        assertEquals(Slice.class, PaymentRepository.class.getMethod("search", String.class,
                LocalDateTime.class, LocalDateTime.class, Pageable.class).getReturnType());
    }
}
