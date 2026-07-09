package com.corebanking.api.service;

import com.corebanking.api.client.AccountClient;
import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import com.corebanking.common.exception.ServiceException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;

@Service
public class ApiService {

    private static final Logger log = LoggerFactory.getLogger(ApiService.class);
    private final AccountClient accountClient;

    public ApiService(AccountClient accountClient) {
        this.accountClient = accountClient;
    }

    public TransferResponse submitTransfer(TransferRequest request) {
        if (request.fromAccount() == null || request.toAccount() == null) {
            throw new ServiceException(HttpStatus.BAD_REQUEST, "fromAccount/toAccount required");
        }
        if (request.amount() == null || request.amount().compareTo(BigDecimal.ZERO) <= 0) {
            throw new ServiceException(HttpStatus.BAD_REQUEST, "amount must be positive");
        }
        log.info("Submitting transfer: from={} to={} amount={} orderId={}",
                request.fromAccount(), request.toAccount(), request.amount(), request.orderId());
        return accountClient.requestTransfer(request);
    }
}
