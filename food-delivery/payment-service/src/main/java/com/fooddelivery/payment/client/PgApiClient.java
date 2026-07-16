package com.fooddelivery.payment.client;

import com.fooddelivery.common.exception.ClientErrorException;
import com.fooddelivery.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestClientResponseException;

import java.math.BigDecimal;
import java.util.Map;

@Component
public class PgApiClient {

    private static final Logger log = LoggerFactory.getLogger(PgApiClient.class);
    private final RestClient pgRestClient;

    public PgApiClient(@Qualifier("pgRestClient") RestClient pgRestClient) {
        this.pgRestClient = pgRestClient;
    }

    // 결제성 호출 — 재시도 증폭 위험 때문에 application.yml에서 max-attempts=2로 낮춰둠.
    @SuppressWarnings("unchecked")
    @CircuitBreaker(name = "pg", fallbackMethod = "payFallback")
    @Retry(name = "pg")
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
            // 4xx는 하류의 정상 업무 거절 — 502로 바꾸지 않고 그대로 전파한다.
            if (ex instanceof RestClientResponseException rex && rex.getStatusCode().is4xxClientError()) {
                throw new ClientErrorException(HttpStatus.valueOf(rex.getStatusCode().value()),
                        "External PG call failed: " + ex.getMessage());
            }
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "External PG call failed: " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private Map<String, Object> payFallback(Long orderId, BigDecimal amount, String pgProvider, Throwable ex) {
        // 4xx는 하류의 정상 업무 거절 — 502로 바꾸지 않고 그대로 전파한다.
        if (ex instanceof ServiceException se && se.getStatus().is4xxClientError()) {
            throw se;
        }
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "PG mock unavailable: " + ex.getMessage());
    }
}
