package com.fooddelivery.order.client;

import com.fooddelivery.common.dto.PaymentRequest;
import com.fooddelivery.common.dto.PaymentResponse;
import com.fooddelivery.common.exception.ClientErrorException;
import com.fooddelivery.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestClientResponseException;

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
            // 4xx는 하류의 정상 업무 거절 — 502로 바꾸지 않고 그대로 전파한다.
            if (ex instanceof RestClientResponseException rex && rex.getStatusCode().is4xxClientError()) {
                throw new ClientErrorException(HttpStatus.valueOf(rex.getStatusCode().value()),
                        "Payment service failed: " + ex.getMessage());
            }
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Payment service failed: " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private PaymentResponse processPaymentFallback(Long orderId, BigDecimal amount, String pgProvider, Throwable ex) {
        // 4xx는 하류의 정상 업무 거절 — 502로 바꾸지 않고 그대로 전파한다.
        if (ex instanceof ServiceException se && se.getStatus().is4xxClientError()) {
            throw se;
        }
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "Payment service unavailable: " + ex.getMessage());
    }
}
