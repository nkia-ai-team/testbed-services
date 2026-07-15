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
        // 4xx는 하류의 정상 업무 거절(재고 부족 409 등)이지 가용성 장애가 아니다 —
        // 502로 바꾸지 않고 그대로 전파한다.
        if (ex instanceof ServiceException se && se.getStatus().is4xxClientError()) {
            throw se;
        }
        throw new ServiceException(HttpStatus.BAD_GATEWAY,
                "Product service unavailable for product " + productId + ": " + ex.getMessage());
    }
}
