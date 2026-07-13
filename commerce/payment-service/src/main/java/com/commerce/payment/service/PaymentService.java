package com.commerce.payment.service;

import com.commerce.common.dto.PaymentEvent;
import com.commerce.common.dto.PaymentRequest;
import com.commerce.common.dto.PaymentResponse;
import com.commerce.common.dto.PaymentSummaryResponse;
import com.commerce.common.dto.SettlementSummaryResponse;
import com.commerce.common.outbox.OutboxPublisher;
import com.commerce.payment.client.BankingTransferClient;
import com.commerce.payment.client.BankingTransferResponse;
import com.commerce.payment.client.PgApiClient;
import com.commerce.payment.entity.Payment;
import com.commerce.payment.entity.PaymentLog;
import com.commerce.payment.repository.PaymentLogRepository;
import com.commerce.payment.repository.PaymentRepository;
import com.commerce.payment.repository.SettlementSummaryRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.Pageable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

@Service
public class PaymentService {

    private final PaymentRepository paymentRepository;
    private final PaymentLogRepository paymentLogRepository;
    private final SettlementSummaryRepository settlementSummaryRepository;
    private final PgApiClient pgApiClient;
    private final BankingTransferClient bankingTransferClient;
    private final OutboxPublisher outboxPublisher;
    private final String paymentsTopic;

    public PaymentService(PaymentRepository paymentRepository,
                          PaymentLogRepository paymentLogRepository,
                          SettlementSummaryRepository settlementSummaryRepository,
                          PgApiClient pgApiClient,
                          BankingTransferClient bankingTransferClient,
                          OutboxPublisher outboxPublisher,
                          @Value("${topics.payments}") String paymentsTopic) {
        this.paymentRepository = paymentRepository;
        this.paymentLogRepository = paymentLogRepository;
        this.settlementSummaryRepository = settlementSummaryRepository;
        this.pgApiClient = pgApiClient;
        this.bankingTransferClient = bankingTransferClient;
        this.outboxPublisher = outboxPublisher;
        this.paymentsTopic = paymentsTopic;
    }

    @Transactional
    public PaymentResponse processPayment(PaymentRequest request) {
        Payment payment = new Payment();
        payment.setOrderId(request.orderId());
        payment.setAmount(request.amount());
        payment.setMethod(request.method() != null ? request.method() : "CARD");
        paymentRepository.save(payment);

        logAction(payment, "PG_REQUEST", "Requesting payment to PG API");

        Map<String, Object> pgResponse = pgApiClient.requestPayment(
                request.orderId(), request.amount(), payment.getMethod());

        String transactionId = (String) pgResponse.get("transaction_id");
        String pgStatus = (String) pgResponse.get("status");

        payment.setStatus(pgStatus);
        payment.setPgTransactionId(transactionId);
        paymentRepository.save(payment);

        logAction(payment, "PG_RESPONSE", "PG response: " + pgStatus + ", txId: " + transactionId);

        // cross-domain: 결제 정산을 core-banking 이체로 위임한다(같은 trace 로 이어짐).
        logAction(payment, "BANKING_TRANSFER_REQUEST", "Requesting settlement transfer to core-banking");
        BankingTransferResponse transfer = bankingTransferClient.transfer(
                request.orderId(), request.amount());
        logAction(payment, "BANKING_TRANSFER_RESPONSE",
                "Banking transfer: " + (transfer != null ? transfer.status() : "no-response"));

        // outbox 발행: 결제 승인/실패 이벤트 → commerce.payments (order-service가 상태 보정에 구독)
        outboxPublisher.publish(paymentsTopic, "PAYMENT", String.valueOf(payment.getId()),
                "PAYMENT_" + payment.getStatus(),
                new PaymentEvent(payment.getId(), payment.getOrderId(), payment.getAmount(),
                        payment.getStatus(), payment.getPgTransactionId()));

        return new PaymentResponse(payment.getId(), payment.getStatus(), transactionId);
    }

    // §7 신규 — 응답은 배열(List)로 통일한다(레포 안의 다른 목록 API들과 동일 관례).
    @Transactional(readOnly = true)
    public List<PaymentSummaryResponse> list(String status, LocalDateTime from, LocalDateTime to, Pageable pageable) {
        return paymentRepository.search(status, from, to, pageable).stream()
                .map(this::toSummary)
                .toList();
    }

    @Transactional(readOnly = true)
    public List<SettlementSummaryResponse> getSettlements(Pageable pageable) {
        return settlementSummaryRepository.findAll(pageable).stream()
                .map(s -> new SettlementSummaryResponse(s.getId(), s.getPeriodStart(), s.getPeriodEnd(),
                        s.getPaymentCount(), s.getTotalAmount(), s.getBankingTransferStatus(),
                        s.getBankingTransferRef(), s.getCreatedAt()))
                .toList();
    }

    private PaymentSummaryResponse toSummary(Payment payment) {
        return new PaymentSummaryResponse(payment.getId(), payment.getOrderId(), payment.getAmount(),
                payment.getMethod(), payment.getStatus(), payment.getPgTransactionId(),
                payment.getSettledAt(), payment.getCreatedAt());
    }

    private void logAction(Payment payment, String action, String detail) {
        PaymentLog log = new PaymentLog();
        log.setPayment(payment);
        log.setAction(action);
        log.setDetail(detail);
        paymentLogRepository.save(log);
    }
}
