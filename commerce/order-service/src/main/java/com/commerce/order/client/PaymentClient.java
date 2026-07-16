package com.commerce.order.client;

import com.commerce.common.dto.PaymentRequest;
import com.commerce.common.dto.PaymentResponse;
import com.commerce.common.exception.ClientErrorException;
import com.commerce.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.math.BigDecimal;

@Component
public class PaymentClient {

    private final RestClient paymentRestClient;

    public PaymentClient(@Qualifier("paymentRestClient") RestClient paymentRestClient) {
        this.paymentRestClient = paymentRestClient;
    }

    @CircuitBreaker(name = "paymentClient", fallbackMethod = "requestPaymentFallback")
    @Retry(name = "paymentClient")
    public PaymentResponse requestPayment(Long orderId, BigDecimal amount, String method) {
        try {
            return paymentRestClient.post()
                    .uri("/api/payments")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(new PaymentRequest(orderId, amount, method))
                    .retrieve()
                    .body(PaymentResponse.class);
        } catch (RestClientResponseException ex) {
            HttpStatus status = HttpStatus.valueOf(ex.getStatusCode().value());
            // 4xx는 하류의 정상 업무 거절 — CB/Retry 실패 집계 대상에서 제외한다.
            if (status.is4xxClientError()) {
                throw new ClientErrorException(status, "Payment service error: " + ex.getMessage());
            }
            throw new ServiceException(status, "Payment service error: " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private PaymentResponse requestPaymentFallback(Long orderId, BigDecimal amount, String method, Throwable ex) {
        // 4xx는 하류의 정상 업무 거절(결제 거절 등) — 502로 바꾸지 않고 그대로 전파한다.
        if (ex instanceof ServiceException se && se.getStatus().is4xxClientError()) {
            throw se;
        }
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "Payment service unavailable: " + ex.getMessage());
    }
}
