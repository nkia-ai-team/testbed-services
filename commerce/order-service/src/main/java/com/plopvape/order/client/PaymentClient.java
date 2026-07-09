package com.plopvape.order.client;

import com.plopvape.common.dto.PaymentRequest;
import com.plopvape.common.dto.PaymentResponse;
import com.plopvape.common.exception.ServiceException;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.math.BigDecimal;

@Component
public class PaymentClient {

    private final RestClient paymentRestClient;

    public PaymentClient(@Qualifier("paymentRestClient") RestClient paymentRestClient) {
        this.paymentRestClient = paymentRestClient;
    }

    public PaymentResponse requestPayment(Long orderId, BigDecimal amount, String method) {
        try {
            return paymentRestClient.post()
                    .uri("/api/payments")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(new PaymentRequest(orderId, amount, method))
                    .retrieve()
                    .body(PaymentResponse.class);
        } catch (RestClientResponseException ex) {
            throw new ServiceException(HttpStatus.valueOf(ex.getStatusCode().value()),
                    "Payment service error: " + ex.getMessage());
        }
    }
}
