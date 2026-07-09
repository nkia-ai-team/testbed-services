package com.plopvape.product.client;

import com.plopvape.common.dto.InventoryReserveRequest;
import com.plopvape.common.dto.InventoryReserveResponse;
import com.plopvape.common.exception.ServiceException;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.util.Map;

@Component
public class InventoryClient {

    private final RestClient inventoryRestClient;

    public InventoryClient(RestClient inventoryRestClient) {
        this.inventoryRestClient = inventoryRestClient;
    }

    public int getStock(Long productId) {
        try {
            @SuppressWarnings("unchecked")
            Map<String, Object> response = inventoryRestClient.get()
                    .uri("/api/inventory/{productId}", productId)
                    .retrieve()
                    .body(Map.class);

            return response != null ? (int) response.get("stock") : 0;
        } catch (RestClientResponseException ex) {
            throw new ServiceException(HttpStatus.valueOf(ex.getStatusCode().value()),
                    "Inventory service error: " + ex.getMessage());
        }
    }

    public InventoryReserveResponse reserve(Long productId, int quantity) {
        try {
            var request = new InventoryReserveRequest(productId, quantity);
            return inventoryRestClient.post()
                    .uri("/api/inventory/reserve")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(request)
                    .retrieve()
                    .body(InventoryReserveResponse.class);
        } catch (RestClientResponseException ex) {
            throw new ServiceException(HttpStatus.valueOf(ex.getStatusCode().value()),
                    "Inventory reserve failed: " + ex.getMessage());
        }
    }
}
