package com.commerce.order.client;

import com.commerce.common.dto.ReserveStockRequest;
import com.commerce.common.dto.ReserveStockResponse;
import com.commerce.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

@Component
public class ProductClient {

    private final RestClient productRestClient;

    public ProductClient(@Qualifier("productRestClient") RestClient productRestClient) {
        this.productRestClient = productRestClient;
    }

    @CircuitBreaker(name = "productClient", fallbackMethod = "reserveStockFallback")
    @Retry(name = "productClient")
    public ReserveStockResponse reserveStock(Long productId, int quantity) {
        try {
            return productRestClient.post()
                    .uri("/api/products/{id}/reserve-stock", productId)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(new ReserveStockRequest(quantity))
                    .retrieve()
                    .body(ReserveStockResponse.class);
        } catch (RestClientResponseException ex) {
            throw new ServiceException(HttpStatus.valueOf(ex.getStatusCode().value()),
                    "Product service error for product " + productId + ": " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private ReserveStockResponse reserveStockFallback(Long productId, int quantity, Throwable ex) {
        throw new ServiceException(HttpStatus.BAD_GATEWAY,
                "Product service unavailable for product " + productId + ": " + ex.getMessage());
    }
}
