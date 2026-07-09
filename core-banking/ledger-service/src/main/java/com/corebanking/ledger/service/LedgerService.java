package com.corebanking.ledger.service;

import com.corebanking.ledger.entity.LedgerEntry;
import com.corebanking.ledger.repository.LedgerEntryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;

@Service
public class LedgerService {

    private static final Logger log = LoggerFactory.getLogger(LedgerService.class);

    private final LedgerEntryRepository ledgerEntryRepository;

    public LedgerService(LedgerEntryRepository ledgerEntryRepository) {
        this.ledgerEntryRepository = ledgerEntryRepository;
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
    }
}
