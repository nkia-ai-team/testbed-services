package com.corebanking.account.service;

import com.corebanking.account.client.TransferClient;
import com.corebanking.account.entity.Account;
import com.corebanking.account.repository.AccountRepository;
import com.corebanking.common.dto.AccountResponse;
import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import com.corebanking.common.exception.ServiceException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class AccountService {

    private static final Logger log = LoggerFactory.getLogger(AccountService.class);

    private final AccountRepository accountRepository;
    private final TransferClient transferClient;

    public AccountService(AccountRepository accountRepository, TransferClient transferClient) {
        this.accountRepository = accountRepository;
        this.transferClient = transferClient;
    }

    @Transactional(readOnly = true)
    public AccountResponse getAccount(String id) {
        Account account = accountRepository.findById(id)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Account not found: " + id));
        return toResponse(account);
    }

    /**
     * 계좌 검증(존재/상태/잔액) 후 transfer-service 로 이체 실행을 위임한다.
     * 실제 잔액 차감과 lock 은 transfer-service 트랜잭션에서 수행.
     */
    @Transactional(readOnly = true)
    public TransferResponse validateAndForward(TransferRequest request) {
        Account from = accountRepository.findById(request.fromAccount())
                .orElseThrow(() -> new ServiceException(HttpStatus.BAD_REQUEST,
                        "From account not found: " + request.fromAccount()));
        Account to = accountRepository.findById(request.toAccount())
                .orElseThrow(() -> new ServiceException(HttpStatus.BAD_REQUEST,
                        "To account not found: " + request.toAccount()));

        if (!"ACTIVE".equalsIgnoreCase(from.getStatus())) {
            throw new ServiceException(HttpStatus.BAD_REQUEST, "From account is " + from.getStatus());
        }
        if (!"ACTIVE".equalsIgnoreCase(to.getStatus())) {
            throw new ServiceException(HttpStatus.BAD_REQUEST, "To account is " + to.getStatus());
        }
        if (from.getBalance().compareTo(request.amount()) < 0) {
            log.info("Transfer rejected: insufficient balance account={} balance={} amount={}",
                    from.getId(), from.getBalance(), request.amount());
            throw new ServiceException(HttpStatus.BAD_REQUEST, "Insufficient balance");
        }

        log.info("Account validation passed: from={} to={} amount={}, forwarding to transfer-service",
                from.getId(), to.getId(), request.amount());
        return transferClient.executeTransfer(request);
    }

    private AccountResponse toResponse(Account a) {
        return new AccountResponse(a.getId(), a.getHolder(), a.getBalance(), a.getStatus());
    }
}
