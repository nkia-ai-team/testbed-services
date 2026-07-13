package com.commerce.order.client;

import com.commerce.common.dto.InventoryReleaseRequest;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

@Component
public class InventoryClient {

    private static final Logger log = LoggerFactory.getLogger(InventoryClient.class);
    private final RestClient inventoryRestClient;

    public InventoryClient(@Qualifier("inventoryRestClient") RestClient inventoryRestClient) {
        this.inventoryRestClient = inventoryRestClient;
    }

    // 보상 트랜잭션(재고 해제) — 실패해도 예외를 던지지 않고 로그만 남긴다(기존 동작 유지).
    @CircuitBreaker(name = "inventoryClient", fallbackMethod = "releaseStockFallback")
    @Retry(name = "inventoryClient")
    public void releaseStock(Long productId, int quantity) {
        try {
            inventoryRestClient.post()
                    .uri("/api/inventory/release")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(new InventoryReleaseRequest(productId, quantity))
                    .retrieve()
                    .toBodilessEntity();
            log.info("Released stock: productId={}, quantity={}", productId, quantity);
        } catch (Exception ex) {
            log.error("Failed to release stock for product {}: {}", productId, ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private void releaseStockFallback(Long productId, int quantity, Throwable ex) {
        log.error("Inventory service unavailable, could not release stock for product {}: {}", productId, ex.getMessage());
    }
}
