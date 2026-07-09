package com.plopvape.payment.client;

import com.plopvape.common.exception.ServiceException;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.math.BigDecimal;
import java.util.Map;

@Component
public class PgApiClient {

    private final RestClient pgRestClient;

    public PgApiClient(RestClient pgRestClient) {
        this.pgRestClient = pgRestClient;
    }

    @SuppressWarnings("unchecked")
    public Map<String, Object> requestPayment(Long orderId, BigDecimal amount, String method) {
        try {
            var body = Map.of(
                    "order_id", orderId,
                    "amount", amount,
                    "method", method
            );

            return pgRestClient.post()
                    .uri("/v1/payments")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(body)
                    .retrieve()
                    .body(Map.class);
        } catch (RestClientException ex) {
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "PG API call failed: " + ex.getMessage());
        }
    }
}
