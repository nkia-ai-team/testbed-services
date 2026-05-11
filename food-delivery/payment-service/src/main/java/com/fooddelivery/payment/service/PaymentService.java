package com.fooddelivery.payment.service;

import com.fooddelivery.common.dto.OrderResponse;
import com.fooddelivery.common.dto.PaymentRequest;
import com.fooddelivery.common.dto.PaymentResponse;
import com.fooddelivery.common.exception.ServiceException;
import com.fooddelivery.payment.client.OrderClient;
import com.fooddelivery.payment.client.PgApiClient;
import com.fooddelivery.payment.entity.Payment;
import com.fooddelivery.payment.repository.PaymentRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.Map;

@Service
public class PaymentService {

    private static final Logger log = LoggerFactory.getLogger(PaymentService.class);

    private final PaymentRepository paymentRepository;
    private final OrderClient orderClient;
    private final PgApiClient pgApiClient;

    public PaymentService(PaymentRepository paymentRepository,
                          OrderClient orderClient,
                          PgApiClient pgApiClient) {
        this.paymentRepository = paymentRepository;
        this.orderClient = orderClient;
        this.pgApiClient = pgApiClient;
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
            throw ex;
        }

        Object pgStatus = pgResponse != null ? pgResponse.get("status") : null;
        payment.setStatus(pgStatus != null ? pgStatus.toString() : "APPROVED");
        payment.setProcessedAt(LocalDateTime.now());
        paymentRepository.save(payment);

        log.info("Processed payment id={}, order={}, amount={}, status={}",
                payment.getId(), payment.getOrderId(), payment.getAmount(), payment.getStatus());
        return toResponse(payment);
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
