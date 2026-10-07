package com.corebanking.ledger.batch;

import com.corebanking.ledger.event.LedgerEventPublisher;
import com.corebanking.ledger.repository.LedgerEntryRepository;
import com.corebanking.ledger.service.LedgerService;
import org.junit.jupiter.api.Test;
import org.springframework.data.domain.PageRequest;

import java.time.LocalDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyCollection;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 원장 정리가 지켜야 할 핵심은 "한 이체의 출금·입금 두 줄이 함께 사라진다"이다. 한쪽만
 * 지워지면 대사 배치가 거짓 불일치 WARN 을 내고, 그 WARN 은 장애 주입 시나리오의 관측
 * 대상이다. 그래서 줄이 아니라 이체 참조번호로 지우는지를 고정한다.
 */
class LedgerRetentionTest {

    private final LedgerEntryRepository entries = mock(LedgerEntryRepository.class);
    private final LedgerService service = new LedgerService(entries, mock(LedgerEventPublisher.class));

    private LedgerEntryRepository.Head head(String transferRef, LocalDateTime createdAt) {
        LedgerEntryRepository.Head head = mock(LedgerEntryRepository.Head.class);
        when(head.getTransferRef()).thenReturn(transferRef);
        when(head.getCreatedAt()).thenReturn(createdAt);
        return head;
    }

    private void oldest(LedgerEntryRepository.Head... rows) {
        when(entries.findOldest(any())).thenReturn(List.of(rows));
    }

    @Test
    void entries_are_deleted_by_transfer_ref_so_both_sides_of_a_pair_go_together() {
        LocalDateTime now = LocalDateTime.now();
        // t-1 은 출금·입금 두 줄이 모두 앞머리에 있다. 참조번호는 한 번만 넘어가야 한다.
        oldest(head("t-1", now.minusDays(100)), head("t-1", now.minusDays(100)),
                head("t-2", now.minusDays(95)), head("t-3", now.minusDays(10)));
        when(entries.deleteByTransferRefIn(anyCollection())).thenReturn(4);

        int purged = service.purgeOlderThan(90, 500);

        assertEquals(4, purged);
        verify(entries).deleteByTransferRefIn(List.of("t-1", "t-2"));
    }

    @Test
    void a_pair_split_by_the_batch_boundary_is_still_deleted_whole() {
        LocalDateTime now = LocalDateTime.now();
        // 묶음 끝에 출금 줄만 걸렸다. 참조번호로 지우므로 묶음 밖의 입금 줄도 같은 문장에서 지워진다.
        oldest(head("t-9", now.minusDays(120)));
        when(entries.deleteByTransferRefIn(anyCollection())).thenReturn(2);

        assertEquals(2, service.purgeOlderThan(90, 1));

        verify(entries).findOldest(PageRequest.of(0, 1));
        verify(entries).deleteByTransferRefIn(List.of("t-9"));
    }

    @Test
    void no_delete_statement_is_issued_when_every_head_row_is_inside_the_retention_period() {
        LocalDateTime now = LocalDateTime.now();
        oldest(head("t-1", now.minusDays(89)), head("t-2", now.minusDays(1)));

        assertEquals(0, service.purgeOlderThan(90, 500));

        verify(entries, never()).deleteByTransferRefIn(anyCollection());
    }

    @Test
    void the_switch_stops_the_batch_before_it_touches_the_table() {
        new LedgerRetentionBatch(service, false, 90, 500).purge();

        verify(entries, never()).findOldest(any());
    }

    @Test
    void a_failing_purge_does_not_escape_into_the_scheduler() {
        // F14-P 는 주입 중 이 테이블을 읽기 전용으로 바꾼다.
        when(entries.findOldest(any())).thenThrow(new IllegalStateException("ORA-12081: table is read only"));

        new LedgerRetentionBatch(service, true, 90, 500).purge();
    }
}
