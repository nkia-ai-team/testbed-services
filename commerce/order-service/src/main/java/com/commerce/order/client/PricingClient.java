package com.commerce.order.client;

import com.commerce.common.dto.QuoteRequest;
import com.commerce.common.dto.QuoteResponse;
import com.commerce.common.exception.ClientErrorException;
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
public class PricingClient {

    private final RestClient pricingRestClient;

    public PricingClient(@Qualifier("pricingRestClient") RestClient pricingRestClient) {
        this.pricingRestClient = pricingRestClient;
    }

    @CircuitBreaker(name = "pricingClient", fallbackMethod = "calculateQuoteFallback")
    @Retry(name = "pricingClient")
    public QuoteResponse calculateQuote(QuoteRequest request) {
        try {
            return pricingRestClient.post()
                    .uri("/api/pricing/quote")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(request)
                    .retrieve()
                    .body(QuoteResponse.class);
        } catch (RestClientResponseException ex) {
            HttpStatus status = HttpStatus.valueOf(ex.getStatusCode().value());
            // 4xx는 하류의 정상 업무 거절 — CB/Retry 실패 집계 대상에서 제외한다.
            if (status.is4xxClientError()) {
                throw new ClientErrorException(status, "Pricing service error: " + ex.getMessage());
            }
            throw new ServiceException(status, "Pricing service error: " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private QuoteResponse calculateQuoteFallback(QuoteRequest request, Throwable ex) {
        // 4xx는 하류의 정상 업무 거절(잘못된 쿠폰 등) — 502로 바꾸지 않고 그대로 전파한다.
        if (ex instanceof ServiceException se && se.getStatus().is4xxClientError()) {
            throw se;
        }
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "Pricing service unavailable: " + ex.getMessage());
    }
}
