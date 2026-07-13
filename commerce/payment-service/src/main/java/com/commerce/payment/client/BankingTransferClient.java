package com.commerce.payment.client;

import com.commerce.common.exception.ServiceException;
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
    // 이체 대상은 banking 시드에 실존하는 고정 가맹점 계좌여야 한다 — 이전의 "merchant-<orderId>" 는
    // 주문마다 미존재 계좌라 banking 계좌 검증에서 전부 400 (109 실배포에서 발견, 2026-07-13).
    // 주문 추적은 orderId 필드가 담당하므로 계좌를 동적으로 만들 이유가 없다.
    private static final String MERCHANT_ACCOUNT = "commerce-merchant";

    private final RestClient bankingTransferRestClient;

    public BankingTransferClient(@Qualifier("bankingTransferRestClient") RestClient bankingTransferRestClient) {
        this.bankingTransferRestClient = bankingTransferRestClient;
    }

    public BankingTransferResponse transfer(Long orderId, BigDecimal amount) {
        try {
            var request = new BankingTransferRequest(
                    SETTLEMENT_ACCOUNT,
                    MERCHANT_ACCOUNT,
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
