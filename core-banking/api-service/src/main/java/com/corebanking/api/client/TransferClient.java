package com.corebanking.api.client;

import com.corebanking.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/**
 * 이체 조회 프록시. 쓰기(POST)는 account-service 체인(AccountClient)을 타지만,
 * 조회는 원장 체인을 거칠 이유가 없어 transfer-service의 GET /api/transfers를 직접 부른다.
 * 응답 DTO(TransferSummaryResponse)는 transfer-service 로컬 타입이라 여기서 역직렬화하지
 * 않고 JSON을 그대로 통과시킨다.
 */
@Component
public class TransferClient {

    private static final Logger log = LoggerFactory.getLogger(TransferClient.class);
    private final RestClient transferRestClient;

    public TransferClient(RestClient transferRestClient) {
        this.transferRestClient = transferRestClient;
    }

    @CircuitBreaker(name = "transferClient", fallbackMethod = "listTransfersFallback")
    @Retry(name = "transferClient")
    public String listTransfers(MultiValueMap<String, String> params) {
        try {
            return transferRestClient.get()
                    .uri(builder -> builder.path("/api/transfers").queryParams(params).build())
                    .retrieve()
                    .body(String.class);
        } catch (RestClientException ex) {
            log.error("Transfer service list call failed: {}", ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Transfer service failed: " + ex.getMessage());
        }
    }

    private String listTransfersFallback(MultiValueMap<String, String> params, Throwable ex) {
        log.error("Transfer service circuit open/exhausted: {}", ex.getMessage());
        throw new ServiceException(HttpStatus.BAD_GATEWAY,
                "Transfer service unavailable: " + ex.getMessage());
    }
}
