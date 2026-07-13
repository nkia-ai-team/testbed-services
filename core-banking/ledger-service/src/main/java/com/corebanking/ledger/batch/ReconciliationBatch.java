package com.corebanking.ledger.batch;

import com.corebanking.ledger.service.LedgerService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;

/**
 * 원장 대사(reconciliation) 배치. 복식부기 원칙상 DEBIT 합계 - CREDIT 합계는 항상 0 이어야 한다.
 * 불일치가 발견되면 WARN 으로 남긴다(장애 주입 시나리오의 관측 대상).
 */
@Component
public class ReconciliationBatch {

    private static final Logger log = LoggerFactory.getLogger(ReconciliationBatch.class);

    private final LedgerService ledgerService;

    public ReconciliationBatch(LedgerService ledgerService) {
        this.ledgerService = ledgerService;
    }

    @Scheduled(fixedDelayString = "${ledger.reconciliation.interval-ms:600000}")
    public void reconcile() {
        log.info("Ledger reconciliation batch started");
        BigDecimal imbalance = ledgerService.sumImbalance();
        if (imbalance.compareTo(BigDecimal.ZERO) != 0) {
            log.warn("Ledger reconciliation MISMATCH detected: imbalance={}", imbalance);
        }
        log.info("Ledger reconciliation batch finished: imbalance={}", imbalance);
    }
}
