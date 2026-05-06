package com.fooddelivery.payment.client;

import com.fooddelivery.common.dto.OrderResponse;
import com.fooddelivery.common.exception.ServiceException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

@Component
public class OrderClient {

    private static final Logger log = LoggerFactory.getLogger(OrderClient.class);
    private final RestClient orderRestClient;

    public OrderClient(@Qualifier("orderRestClient") RestClient orderRestClient) {
        this.orderRestClient = orderRestClient;
    }

    public OrderResponse getOrder(Long orderId) {
        try {
            return orderRestClient.get()
                    .uri("/api/orders/{id}", orderId)
                    .retrieve()
                    .body(OrderResponse.class);
        } catch (RestClientException ex) {
            log.error("Failed to fetch order {}: {}", orderId, ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Order lookup failed: " + ex.getMessage());
        }
    }
}
