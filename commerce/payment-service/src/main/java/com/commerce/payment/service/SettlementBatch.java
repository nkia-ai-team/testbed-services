package com.commerce.payment.service;

import com.commerce.payment.client.BankingTransferClient;
import com.commerce.payment.client.BankingTransferResponse;
import com.commerce.payment.entity.SettlementSummary;
import com.commerce.payment.repository.PaymentRepository;
import com.commerce.payment.repository.SettlementSummaryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

/**
 * cross-domain 경로 ② (§2): commerce.payment 정산 → banking 원장.
 * 매시 정각 미정산 결제를 집계해 core-banking으로 이체 1건을 실행한다.
 * 이체 실패해도 summary는 기록하고 payments는 미정산 상태로 남겨, 다음 주기 쿼리에 자동으로
 * 다시 포함되게 한다(재이체 시 미정산분 누적 포함).
 */
@Component
public class SettlementBatch {

    private static final Logger log = LoggerFactory.getLogger(SettlementBatch.class);
    private static final List<String> SETTLEABLE_STATUSES = List.of("SUCCESS", "APPROVED", "COMPLETED");

    private final PaymentRepository paymentRepository;
    private final SettlementSummaryRepository settlementSummaryRepository;
    private final BankingTransferClient bankingTransferClient;

    public SettlementBatch(PaymentRepository paymentRepository,
                            SettlementSummaryRepository settlementSummaryRepository,
                            BankingTransferClient bankingTransferClient) {
        this.paymentRepository = paymentRepository;
        this.settlementSummaryRepository = settlementSummaryRepository;
        this.bankingTransferClient = bankingTransferClient;
    }

    @Scheduled(cron = "0 0 * * * *")
    @Transactional
    public void run() {
        LocalDateTime cutoff = LocalDateTime.now();
        PaymentRepository.SettleableAggregate pending =
                paymentRepository.aggregateSettleable(SETTLEABLE_STATUSES, cutoff);
        long pendingCount = pending.getPaymentCount();

        log.info("Settlement batch started: pendingCount={}", pendingCount);
        if (pendingCount == 0) {
            log.info("Settlement batch finished: nothing to settle");
            return;
        }

        BigDecimal total = pending.getTotalAmount();
        LocalDateTime periodStart = pending.getPeriodStart() != null ? pending.getPeriodStart() : cutoff;

        SettlementSummary summary = new SettlementSummary();
        summary.setPeriodStart(periodStart);
        summary.setPeriodEnd(cutoff);
        summary.setPaymentCount((int) pendingCount);
        summary.setTotalAmount(total);
        settlementSummaryRepository.save(summary);

        try {
            BankingTransferResponse transfer = bankingTransferClient.transfer(summary.getId(), total);
            boolean success = transfer != null && "COMPLETED".equals(transfer.status());
            summary.setBankingTransferStatus(success ? "SUCCESS" : "FAILED");
            summary.setBankingTransferRef(transfer != null ? transfer.transferId() : null);

            if (success) {
                int settled = paymentRepository.markSettled(SETTLEABLE_STATUSES, cutoff, LocalDateTime.now());
                log.info("Settlement marked settled: {} payments", settled);
            }
        } catch (Exception ex) {
            summary.setBankingTransferStatus("FAILED");
            log.error("Settlement banking transfer failed, unsettled payments carry over to next cycle: {}",
                    ex.getMessage());
        }
        settlementSummaryRepository.save(summary);

        log.info("Settlement batch finished: count={}, total={}, transferStatus={}",
                pendingCount, total, summary.getBankingTransferStatus());
    }
}
