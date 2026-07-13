package com.corebanking.ledger.service;

import com.corebanking.ledger.dto.LedgerEntryResponse;
import com.corebanking.ledger.entity.LedgerEntry;
import com.corebanking.ledger.event.LedgerEventPublisher;
import com.corebanking.ledger.repository.LedgerEntryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.domain.Pageable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.List;

@Service
public class LedgerService {

    private static final Logger log = LoggerFactory.getLogger(LedgerService.class);

    private final LedgerEntryRepository ledgerEntryRepository;
    private final LedgerEventPublisher eventPublisher;

    public LedgerService(LedgerEntryRepository ledgerEntryRepository, LedgerEventPublisher eventPublisher) {
        this.ledgerEntryRepository = ledgerEntryRepository;
        this.eventPublisher = eventPublisher;
    }

    /**
     * 이체 이벤트를 복식부기(double-entry)로 원장에 반영한다.
     * 멱등 보장: 같은 transferRef 가 이미 기록됐으면 skip.
     */
    @Transactional
    public void recordTransfer(String transferRef, String fromAccount, String toAccount, BigDecimal amount) {
        if (!ledgerEntryRepository.findByTransferRef(transferRef).isEmpty()) {
            log.info("Ledger already recorded, skipping: transferRef={}", transferRef);
            return;
        }
        ledgerEntryRepository.save(new LedgerEntry(transferRef, fromAccount, "DEBIT", amount));
        ledgerEntryRepository.save(new LedgerEntry(transferRef, toAccount, "CREDIT", amount));
        log.info("Ledger recorded: transferRef={} debit={} credit={} amount={}",
                transferRef, fromAccount, toAccount, amount);
        eventPublisher.publishRecorded(transferRef, fromAccount, toAccount, amount);
    }

    /**
     * 원장 대사(reconciliation) 배치용: 전체 잔액 합(DEBIT - CREDIT)이 0인지 검증한다.
     * 복식부기가 정상이면 항상 0 이어야 한다.
     */
    @Transactional(readOnly = true)
    public BigDecimal sumImbalance() {
        BigDecimal debit = ledgerEntryRepository.sumAmountByDirection("DEBIT");
        BigDecimal credit = ledgerEntryRepository.sumAmountByDirection("CREDIT");
        debit = debit != null ? debit : BigDecimal.ZERO;
        credit = credit != null ? credit : BigDecimal.ZERO;
        return debit.subtract(credit);
    }

    /**
     * 원장 목록 조회(계좌/이체ref/방향 필터 + 선택적 페이지네이션).
     * page/size 둘 다 미지정이면 unpaged 로 조회해 기존 무제한 조회 관례를 유지한다.
     */
    @Transactional(readOnly = true)
    public List<LedgerEntryResponse> search(String accountId, String transferRef, String direction, Pageable pageable) {
        return ledgerEntryRepository.search(accountId, transferRef, direction, pageable)
                .map(this::toResponse)
                .getContent();
    }

    private LedgerEntryResponse toResponse(LedgerEntry e) {
        return new LedgerEntryResponse(e.getId(), e.getTransferRef(), e.getAccountId(),
                e.getDirection(), e.getAmount(), e.getCreatedAt());
    }
}
