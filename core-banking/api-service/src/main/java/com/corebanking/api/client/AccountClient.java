package com.corebanking.api.client;

import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import com.corebanking.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

@Component
public class AccountClient {

    private static final Logger log = LoggerFactory.getLogger(AccountClient.class);
    private final RestClient accountRestClient;

    public AccountClient(RestClient accountRestClient) {
        this.accountRestClient = accountRestClient;
    }

    @CircuitBreaker(name = "accountClient", fallbackMethod = "requestTransferFallback")
    @Retry(name = "accountClient")
    public TransferResponse requestTransfer(TransferRequest request) {
        try {
            return accountRestClient.post()
                    .uri("/api/accounts/transfer")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(request)
                    .retrieve()
                    .body(TransferResponse.class);
        } catch (RestClientException ex) {
            log.error("Account service call failed for order {}: {}", request.orderId(), ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Account service failed: " + ex.getMessage());
        }
    }

    private TransferResponse requestTransferFallback(TransferRequest request, Throwable ex) {
        log.error("Account service circuit open/exhausted for order {}: {}", request.orderId(), ex.getMessage());
        throw new ServiceException(HttpStatus.BAD_GATEWAY,
                "Account service unavailable: " + ex.getMessage());
    }
}
