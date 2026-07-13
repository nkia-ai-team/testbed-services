package com.corebanking.account.client;

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
public class TransferClient {

    private static final Logger log = LoggerFactory.getLogger(TransferClient.class);
    private final RestClient transferRestClient;

    public TransferClient(RestClient transferRestClient) {
        this.transferRestClient = transferRestClient;
    }

    @CircuitBreaker(name = "transferClient", fallbackMethod = "executeTransferFallback")
    @Retry(name = "transferClient")
    public TransferResponse executeTransfer(TransferRequest request) {
        try {
            return transferRestClient.post()
                    .uri("/api/transfers")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(request)
                    .retrieve()
                    .body(TransferResponse.class);
        } catch (RestClientException ex) {
            log.error("Transfer service call failed for order {}: {}", request.orderId(), ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Transfer service failed: " + ex.getMessage());
        }
    }

    private TransferResponse executeTransferFallback(TransferRequest request, Throwable ex) {
        log.error("Transfer service circuit open/exhausted for order {}: {}", request.orderId(), ex.getMessage());
        throw new ServiceException(HttpStatus.BAD_GATEWAY,
                "Transfer service unavailable: " + ex.getMessage());
    }
}
