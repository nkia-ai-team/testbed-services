package com.fooddelivery.payment.client;

import com.fooddelivery.common.dto.OrderResponse;
import com.fooddelivery.common.exception.ClientErrorException;
import com.fooddelivery.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestClientResponseException;

@Component
public class OrderClient {

    private static final Logger log = LoggerFactory.getLogger(OrderClient.class);
    private final RestClient orderRestClient;

    public OrderClient(@Qualifier("orderRestClient") RestClient orderRestClient) {
        this.orderRestClient = orderRestClient;
    }

    @CircuitBreaker(name = "order", fallbackMethod = "getOrderFallback")
    @Retry(name = "order")
    public OrderResponse getOrder(Long orderId) {
        try {
            return orderRestClient.get()
                    .uri("/api/orders/{id}", orderId)
                    .retrieve()
                    .body(OrderResponse.class);
        } catch (RestClientException ex) {
            log.error("Failed to fetch order {}: {}", orderId, ex.getMessage());
            // 4xx는 하류의 정상 업무 거절 — 502로 바꾸지 않고 그대로 전파한다.
            if (ex instanceof RestClientResponseException rex && rex.getStatusCode().is4xxClientError()) {
                throw new ClientErrorException(HttpStatus.valueOf(rex.getStatusCode().value()),
                        "Order lookup failed: " + ex.getMessage());
            }
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Order lookup failed: " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private OrderResponse getOrderFallback(Long orderId, Throwable ex) {
        // 4xx는 하류의 정상 업무 거절 — 502로 바꾸지 않고 그대로 전파한다.
        if (ex instanceof ServiceException se && se.getStatus().is4xxClientError()) {
            throw se;
        }
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "Order service unavailable: " + ex.getMessage());
    }
}
