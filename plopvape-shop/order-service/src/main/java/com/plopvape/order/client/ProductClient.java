package com.plopvape.order.client;

import com.plopvape.common.dto.ReserveStockRequest;
import com.plopvape.common.dto.ReserveStockResponse;
import com.plopvape.common.exception.ServiceException;
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
}
