package com.plopvape.payment.client;

import com.plopvape.common.exception.ServiceException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.math.BigDecimal;

/**
 * cross-domain 호출: commerce-payment → core-banking-transfer.
 * 결제 정산을 core-banking 이체 트랜잭션으로 위임한다. namespace가 분리되어 있어
 * FQDN(services.banking-transfer.base-url)으로 호출하며, W3C traceparent 는 OTel
 * javaagent 가 자동 전파하므로 별도 헤더 코드는 두지 않는다.
 */
@Component
public class BankingTransferClient {

    private static final Logger log = LoggerFactory.getLogger(BankingTransferClient.class);
    private static final String SETTLEMENT_ACCOUNT = "commerce-settlement";

    private final RestClient bankingTransferRestClient;

    public BankingTransferClient(@Qualifier("bankingTransferRestClient") RestClient bankingTransferRestClient) {
        this.bankingTransferRestClient = bankingTransferRestClient;
    }

    public BankingTransferResponse transfer(Long orderId, BigDecimal amount) {
        try {
            var request = new BankingTransferRequest(
                    SETTLEMENT_ACCOUNT,
                    "merchant-" + orderId,
                    amount,
                    String.valueOf(orderId));

            BankingTransferResponse response = bankingTransferRestClient.post()
                    .uri("/api/transfers")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(request)
                    .retrieve()
                    .body(BankingTransferResponse.class);

            log.info("Banking transfer for order {}: {}", orderId,
                    response != null ? response.status() : "no-response");
            return response;
        } catch (RestClientException ex) {
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "core-banking transfer call failed: " + ex.getMessage());
        }
    }
}
