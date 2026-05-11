package com.fooddelivery.order.client;

import com.fooddelivery.common.dto.PaymentRequest;
import com.fooddelivery.common.dto.PaymentResponse;
import com.fooddelivery.common.exception.ServiceException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.math.BigDecimal;

@Component
public class PaymentClient {

    private static final Logger log = LoggerFactory.getLogger(PaymentClient.class);
    private final RestClient paymentRestClient;

    public PaymentClient(RestClient paymentRestClient) {
        this.paymentRestClient = paymentRestClient;
    }

    public PaymentResponse processPayment(Long orderId, BigDecimal amount, String pgProvider) {
        try {
            return paymentRestClient.post()
                    .uri("/api/payments")
                    .body(new PaymentRequest(orderId, amount, pgProvider))
                    .retrieve()
                    .body(PaymentResponse.class);
        } catch (RestClientException ex) {
            log.error("Failed to process payment for order {}: {}", orderId, ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Payment service failed: " + ex.getMessage());
        }
    }
}
