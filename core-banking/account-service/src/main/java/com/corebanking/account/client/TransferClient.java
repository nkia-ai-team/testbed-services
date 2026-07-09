package com.corebanking.account.client;

import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import com.corebanking.common.exception.ServiceException;
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
}
