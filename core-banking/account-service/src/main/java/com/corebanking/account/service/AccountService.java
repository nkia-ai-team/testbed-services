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
import org.springframework.data.domain.Pageable;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.List;

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
     * 계좌 목록 조회(상태/예금주 검색 + 선택적 페이지네이션).
     * page/size 둘 다 미지정이면 unpaged 로 조회(기존 무제한 조회 관례 유지 — 이 서비스는
     * 목록 API 가 신규라 back-compat 대상은 아니지만 다른 서비스와 관례를 통일한다).
     */
    @Transactional(readOnly = true)
    public List<AccountResponse> search(String status, String holder, Pageable pageable) {
        return accountRepository.search(status, holder, pageable)
                .map(this::toResponse)
                .getContent();
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

    /**
     * 이자 계산/기표 배치용: ACTIVE 계좌 잔액에 일할 이자를 가산한다.
     * dailyRate 는 이미 연이율을 365 로 나눈 일할 비율.
     */
    @Transactional
    public int applyDailyInterest(BigDecimal dailyRate) {
        List<Account> active = accountRepository.findByStatus("ACTIVE");
        int applied = 0;
        for (Account account : active) {
            if (account.getBalance().compareTo(BigDecimal.ZERO) <= 0) {
                continue;
            }
            BigDecimal interest = account.getBalance().multiply(dailyRate).setScale(2, RoundingMode.HALF_UP);
            if (interest.compareTo(BigDecimal.ZERO) <= 0) {
                continue;
            }
            account.setBalance(account.getBalance().add(interest));
            accountRepository.save(account);
            applied++;
        }
        return applied;
    }

    private AccountResponse toResponse(Account a) {
        return new AccountResponse(a.getId(), a.getHolder(), a.getBalance(), a.getStatus());
    }
}
