package com.fooddelivery.order.client;

import com.fooddelivery.common.dto.PaymentRequest;
import com.fooddelivery.common.dto.PaymentResponse;
import com.fooddelivery.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.math.BigDecimal;

@Component
public class PaymentClient {

    private static final Logger log = LoggerFactory.getLogger(PaymentClient.class);
    private final RestClient paymentRestClient;

    public PaymentClient(RestClient paymentRestClient) {
        this.paymentRestClient = paymentRestClient;
    }

    // 결제성 호출 — 재시도 증폭(중복결제) 위험 때문에 application.yml에서 max-attempts=2로 낮춰둠.
    @CircuitBreaker(name = "payment", fallbackMethod = "processPaymentFallback")
    @Retry(name = "payment")
    public PaymentResponse processPayment(Long orderId, BigDecimal amount, String pgProvider) {
        try {
            return paymentRestClient.post()
                    .uri("/api/payments")
                    .body(new PaymentRequest(orderId, amount, pgProvider))
                    .retrieve()
                    .body(PaymentResponse.class);
        } catch (RestClientException ex) {
            log.error("Failed to process payment for order {}: {}", orderId, ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Payment service failed: " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private PaymentResponse processPaymentFallback(Long orderId, BigDecimal amount, String pgProvider, Throwable ex) {
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "Payment service unavailable: " + ex.getMessage());
    }
}
