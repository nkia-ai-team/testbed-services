package com.fooddelivery.payment.client;

import com.fooddelivery.common.exception.ServiceException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.math.BigDecimal;
import java.util.Map;

@Component
public class PgApiClient {

    private static final Logger log = LoggerFactory.getLogger(PgApiClient.class);
    private final RestClient pgRestClient;

    public PgApiClient(@Qualifier("pgRestClient") RestClient pgRestClient) {
        this.pgRestClient = pgRestClient;
    }

    @SuppressWarnings("unchecked")
    public Map<String, Object> pay(Long orderId, BigDecimal amount, String pgProvider) {
        try {
            var body = Map.of(
                    "order_id", orderId,
                    "amount", amount,
                    "pg_provider", pgProvider != null ? pgProvider : "default"
            );
            return pgRestClient.post()
                    .uri("/pay")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(body)
                    .retrieve()
                    .body(Map.class);
        } catch (RestClientException ex) {
            log.error("PG /pay failed for order={}: {}", orderId, ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "External PG call failed: " + ex.getMessage());
        }
    }
}
