package com.corebanking.transfer.batch;

import com.corebanking.transfer.event.TransferEventPublisher;
import com.corebanking.transfer.repository.AccountRepository;
import com.corebanking.transfer.repository.TransferRepository;
import com.corebanking.transfer.service.TransferService;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.data.domain.PageRequest;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 보존 기간 정리는 업무 데이터를 지우는 배치다. 고정할 것은 (1) 기준 시각이 정확히
 * 보존 기간만큼 과거인가 (2) 한 주기가 묶음 크기를 넘지 않는가 (3) 지울 것이 없으면
 * 삭제 문장을 내지 않는가 (4) 스위치와 실패 격리다.
 */
class TransferRetentionTest {

    private final TransferRepository transfers = mock(TransferRepository.class);
    private final TransferService service =
            new TransferService(mock(AccountRepository.class), transfers, mock(TransferEventPublisher.class));

    @Test
    void transfers_older_than_the_retention_period_are_deleted_in_one_batch() {
        when(transfers.findIdsCreatedBefore(any(), any())).thenReturn(List.of(11L, 12L, 13L));

        int purged = service.purgeOlderThan(90, 500);

        assertEquals(3, purged);
        verify(transfers).deleteAllByIdInBatch(List.of(11L, 12L, 13L));
    }

    @Test
    void the_cutoff_is_the_retention_period_before_now_and_the_batch_is_bounded() {
        when(transfers.findIdsCreatedBefore(any(), any())).thenReturn(List.of());

        service.purgeOlderThan(90, 200);

        ArgumentCaptor<LocalDateTime> cutoff = ArgumentCaptor.forClass(LocalDateTime.class);
        verify(transfers).findIdsCreatedBefore(cutoff.capture(), eq(PageRequest.of(0, 200)));
        Duration drift = Duration.between(cutoff.getValue(), LocalDateTime.now().minusDays(90)).abs();
        assertTrue(drift.getSeconds() < 5, "기준 시각이 90일 전에서 " + drift + " 벗어났다");
    }

    @Test
    void no_delete_statement_is_issued_when_nothing_is_old_enough() {
        when(transfers.findIdsCreatedBefore(any(), any())).thenReturn(List.of());

        assertEquals(0, service.purgeOlderThan(90, 500));

        verify(transfers, never()).deleteAllByIdInBatch(anyList());
    }

    @Test
    void the_switch_stops_the_batch_before_it_touches_the_table() {
        new TransferRetentionBatch(service, false, 90, 500).purge();

        verify(transfers, never()).findIdsCreatedBefore(any(), any());
    }

    @Test
    void a_failing_purge_does_not_escape_into_the_scheduler() {
        when(transfers.findIdsCreatedBefore(any(), any()))
                .thenThrow(new IllegalStateException("ORA-12081: table is read only"));

        // 던지면 스케줄러가 매 주기 ERROR 스택을 찍는다. 삼키고 다음 주기에 다시 시도한다.
        new TransferRetentionBatch(service, true, 90, 500).purge();
    }
}
