package com.fooddelivery.payment.service;

import com.fooddelivery.payment.entity.Payment;
import com.fooddelivery.payment.entity.SettlementSummary;
import com.fooddelivery.payment.repository.PaymentRepository;
import com.fooddelivery.payment.repository.SettlementSummaryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

/**
 * 매 주기(settlement.poll-interval-ms)마다 미정산 완료 결제를 모아 정산 요약을 남긴다.
 * 이체 대상 원장이 없는 도메인이므로 commerce 정산 배치와 달리 요약 기록만 하고 종료한다.
 */
@Component
public class SettlementBatch {

    private static final Logger log = LoggerFactory.getLogger(SettlementBatch.class);
    private static final List<String> SETTLEABLE_STATUSES = List.of("APPROVED", "COMPLETED", "SUCCESS");

    private final PaymentRepository paymentRepository;
    private final SettlementSummaryRepository settlementSummaryRepository;

    public SettlementBatch(PaymentRepository paymentRepository,
                            SettlementSummaryRepository settlementSummaryRepository) {
        this.paymentRepository = paymentRepository;
        this.settlementSummaryRepository = settlementSummaryRepository;
    }

    @Scheduled(fixedDelayString = "${settlement.poll-interval-ms:3600000}",
            initialDelayString = "${settlement.poll-interval-ms:3600000}")
    @Transactional
    public void run() {
        LocalDateTime cutoff = LocalDateTime.now();
        List<Payment> pending = paymentRepository.findByStatusInAndSettledAtIsNullAndCreatedAtLessThan(
                SETTLEABLE_STATUSES, cutoff);

        log.info("Settlement batch started: pendingCount={}", pending.size());
        if (pending.isEmpty()) {
            log.info("Settlement batch finished: nothing to settle");
            return;
        }

        BigDecimal total = pending.stream().map(Payment::getAmount).reduce(BigDecimal.ZERO, BigDecimal::add);
        LocalDateTime periodStart = pending.stream().map(Payment::getCreatedAt)
                .min(LocalDateTime::compareTo).orElse(cutoff);

        SettlementSummary summary = new SettlementSummary();
        summary.setPeriodStart(periodStart);
        summary.setPeriodEnd(cutoff);
        summary.setPaymentCount(pending.size());
        summary.setTotalAmount(total);
        settlementSummaryRepository.save(summary);

        pending.forEach(p -> p.setSettledAt(cutoff));
        paymentRepository.saveAll(pending);

        log.info("Settlement batch finished: count={}, total={}", pending.size(), total);
    }
}
