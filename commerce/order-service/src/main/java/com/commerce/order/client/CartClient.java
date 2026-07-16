package com.commerce.order.client;

import com.commerce.common.dto.CartResponse;
import com.commerce.common.exception.ClientErrorException;
import com.commerce.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

@Component
public class CartClient {

    private static final Logger log = LoggerFactory.getLogger(CartClient.class);
    private final RestClient cartRestClient;

    public CartClient(@Qualifier("cartRestClient") RestClient cartRestClient) {
        this.cartRestClient = cartRestClient;
    }

    @CircuitBreaker(name = "cartClient", fallbackMethod = "getCartFallback")
    @Retry(name = "cartClient")
    public CartResponse getCart(Long userId) {
        try {
            return cartRestClient.get().uri("/api/carts/{userId}", userId).retrieve().body(CartResponse.class);
        } catch (RestClientResponseException ex) {
            HttpStatus status = HttpStatus.valueOf(ex.getStatusCode().value());
            // 4xx는 하류의 정상 업무 거절 — CB/Retry 실패 집계 대상에서 제외한다.
            if (status.is4xxClientError()) {
                throw new ClientErrorException(status, "Cart service error for user " + userId + ": " + ex.getMessage());
            }
            throw new ServiceException(status, "Cart service error for user " + userId + ": " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private CartResponse getCartFallback(Long userId, Throwable ex) {
        // 4xx는 하류의 정상 업무 거절(빈 장바구니 등) — 502로 바꾸지 않고 그대로 전파한다.
        if (ex instanceof ServiceException se && se.getStatus().is4xxClientError()) {
            throw se;
        }
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "Cart service unavailable: " + ex.getMessage());
    }

    // checkout 성공 후 장바구니 비우기 — best-effort. 실패해도 주문 자체는 이미 성공했으므로
    // 예외를 던지지 않고 로그만 남긴다.
    @CircuitBreaker(name = "cartClient", fallbackMethod = "clearCartFallback")
    @Retry(name = "cartClient")
    public void clearCart(Long userId) {
        try {
            cartRestClient.delete().uri("/api/carts/{userId}", userId).retrieve().toBodilessEntity();
            log.info("Cleared cart after checkout: userId={}", userId);
        } catch (Exception ex) {
            log.warn("Failed to clear cart after checkout (best-effort, non-critical): userId={}, {}", userId, ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private void clearCartFallback(Long userId, Throwable ex) {
        log.warn("Cart service unavailable, could not clear cart (best-effort, non-critical): userId={}, {}", userId, ex.getMessage());
    }
}
