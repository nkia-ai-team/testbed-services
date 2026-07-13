package com.fooddelivery.payment.service;

import com.fooddelivery.common.dto.PaymentEvent;
import com.fooddelivery.common.dto.PaymentRequest;
import com.fooddelivery.common.dto.PaymentResponse;
import com.fooddelivery.common.dto.SettlementSummaryResponse;
import com.fooddelivery.common.exception.ServiceException;
import com.fooddelivery.payment.client.PgApiClient;
import com.fooddelivery.payment.entity.Payment;
import com.fooddelivery.payment.entity.SettlementSummary;
import com.fooddelivery.payment.event.PaymentOutboxPublisher;
import com.fooddelivery.payment.repository.PaymentRepository;
import com.fooddelivery.payment.repository.SettlementSummaryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.Pageable;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

@Service
public class PaymentService {

    private static final Logger log = LoggerFactory.getLogger(PaymentService.class);

    private final PaymentRepository paymentRepository;
    private final SettlementSummaryRepository settlementSummaryRepository;
    private final PgApiClient pgApiClient;
    private final PaymentOutboxPublisher outboxPublisher;
    private final String paymentsTopic;

    public PaymentService(PaymentRepository paymentRepository,
                          SettlementSummaryRepository settlementSummaryRepository,
                          PgApiClient pgApiClient,
                          PaymentOutboxPublisher outboxPublisher,
                          @Value("${topics.payments}") String paymentsTopic) {
        this.paymentRepository = paymentRepository;
        this.settlementSummaryRepository = settlementSummaryRepository;
        this.pgApiClient = pgApiClient;
        this.outboxPublisher = outboxPublisher;
        this.paymentsTopic = paymentsTopic;
    }

    @Transactional
    public PaymentResponse processPayment(PaymentRequest request) {
        // orderId 는 request 그대로 신뢰 (microservice 경계).
        // order-service @Transactional 안에서 fan-out 호출 시 commit 전이라 reverse GET 시 404 race.
        if (request.orderId() == null) {
            throw new ServiceException(HttpStatus.BAD_REQUEST, "orderId required");
        }

        BigDecimal amount = request.amount() != null ? request.amount() : BigDecimal.ZERO;
        String pgProvider = request.pgProvider() != null ? request.pgProvider() : "default";

        Payment payment = new Payment();
        payment.setOrderId(request.orderId());
        payment.setPgProvider(pgProvider);
        payment.setAmount(amount);
        payment.setStatus("PENDING");
        paymentRepository.save(payment);

        Map<String, Object> pgResponse;
        try {
            pgResponse = pgApiClient.pay(request.orderId(), amount, pgProvider);
        } catch (ServiceException ex) {
            payment.setStatus("FAILED");
            payment.setProcessedAt(LocalDateTime.now());
            paymentRepository.save(payment);
            publishEvent(payment);
            throw ex;
        }

        Object pgStatus = pgResponse != null ? pgResponse.get("status") : null;
        payment.setStatus(pgStatus != null ? pgStatus.toString() : "APPROVED");
        payment.setProcessedAt(LocalDateTime.now());
        paymentRepository.save(payment);
        publishEvent(payment);

        log.info("Processed payment id={}, order={}, amount={}, status={}",
                payment.getId(), payment.getOrderId(), payment.getAmount(), payment.getStatus());
        return toResponse(payment);
    }

    // §7 신규 — status/from/to/page/size 필터 조합. 배열 응답(하위호환).
    @Transactional(readOnly = true)
    public List<PaymentResponse> searchPayments(String status, LocalDateTime from, LocalDateTime to, Pageable pageable) {
        return paymentRepository.search(status, from, to, pageable).getContent().stream()
                .map(this::toResponse)
                .toList();
    }

    // §7 신규 — SettlementBatch 가 남긴 정산 요약 목록.
    @Transactional(readOnly = true)
    public List<SettlementSummaryResponse> getSettlements(Pageable pageable) {
        return settlementSummaryRepository.findAllByOrderByPeriodEndDesc(pageable).getContent().stream()
                .map(s -> new SettlementSummaryResponse(
                        s.getId(), s.getPeriodStart(), s.getPeriodEnd(),
                        s.getPaymentCount(), s.getTotalAmount(), s.getCreatedAt()))
                .toList();
    }

    private void publishEvent(Payment payment) {
        try {
            outboxPublisher.publish(paymentsTopic, "PAYMENT", String.valueOf(payment.getId()),
                    "PAYMENT_" + payment.getStatus(),
                    new PaymentEvent(payment.getId(), payment.getOrderId(), payment.getAmount(), payment.getStatus()));
        } catch (Exception ex) {
            log.warn("Failed to record payment outbox event (non-critical): {}", ex.getMessage());
        }
    }

    private PaymentResponse toResponse(Payment p) {
        return new PaymentResponse(
                p.getId(),
                p.getOrderId(),
                p.getPgProvider(),
                p.getAmount(),
                p.getStatus(),
                p.getProcessedAt()
        );
    }
}
