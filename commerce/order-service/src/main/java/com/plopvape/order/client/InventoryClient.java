package com.plopvape.order.client;

import com.plopvape.common.dto.InventoryReleaseRequest;
import com.plopvape.common.exception.ServiceException;
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
}
