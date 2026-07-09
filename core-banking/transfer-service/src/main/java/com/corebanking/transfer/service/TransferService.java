package com.corebanking.transfer.service;

import com.corebanking.common.dto.TransferEvent;
import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import com.corebanking.common.exception.ServiceException;
import com.corebanking.transfer.entity.Account;
import com.corebanking.transfer.entity.Transfer;
import com.corebanking.transfer.event.TransferEventPublisher;
import com.corebanking.transfer.repository.AccountRepository;
import com.corebanking.transfer.repository.TransferRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.UUID;

@Service
public class TransferService {

    private static final Logger log = LoggerFactory.getLogger(TransferService.class);

    private final AccountRepository accountRepository;
    private final TransferRepository transferRepository;
    private final TransferEventPublisher eventPublisher;

    public TransferService(AccountRepository accountRepository,
                           TransferRepository transferRepository,
                           TransferEventPublisher eventPublisher) {
        this.accountRepository = accountRepository;
        this.transferRepository = transferRepository;
        this.eventPublisher = eventPublisher;
    }

    /**
     * 이체 실행. 출금/입금 계좌를 id 오름차순으로 FOR UPDATE lock 하여 deadlock 을 피하고,
     * 잔액 차감/증가 + transfers row 기록 후 Redis 로 이벤트를 발행한다.
     * lock 을 두 계좌에 잡으므로 동시 이체가 몰리면 row-lock 경합이 커진다(장애 표면).
     */
    @Transactional
    public TransferResponse execute(TransferRequest request) {
        if (request.amount() == null || request.amount().compareTo(BigDecimal.ZERO) <= 0) {
            throw new ServiceException(HttpStatus.BAD_REQUEST, "amount must be positive");
        }
        if (request.fromAccount().equals(request.toAccount())) {
            throw new ServiceException(HttpStatus.BAD_REQUEST, "fromAccount and toAccount must differ");
        }

        String transferRef = UUID.randomUUID().toString();

        // deadlock 회피: 항상 작은 id 를 먼저 lock
        String firstId = request.fromAccount().compareTo(request.toAccount()) <= 0
                ? request.fromAccount() : request.toAccount();
        String secondId = firstId.equals(request.fromAccount())
                ? request.toAccount() : request.fromAccount();

        Account first = accountRepository.findByIdForUpdate(firstId)
                .orElseThrow(() -> new ServiceException(HttpStatus.BAD_REQUEST, "Account not found: " + firstId));
        Account second = accountRepository.findByIdForUpdate(secondId)
                .orElseThrow(() -> new ServiceException(HttpStatus.BAD_REQUEST, "Account not found: " + secondId));

        Account from = firstId.equals(request.fromAccount()) ? first : second;
        Account to = firstId.equals(request.toAccount()) ? first : second;

        String status;
        if (from.getBalance().compareTo(request.amount()) < 0) {
            status = "FAILED";
            log.info("Transfer FAILED (insufficient balance): ref={} from={} balance={} amount={}",
                    transferRef, from.getId(), from.getBalance(), request.amount());
        } else {
            from.setBalance(from.getBalance().subtract(request.amount()));
            to.setBalance(to.getBalance().add(request.amount()));
            accountRepository.save(from);
            accountRepository.save(to);
            status = "COMPLETED";
            log.info("Transfer COMPLETED: ref={} from={} to={} amount={}",
                    transferRef, from.getId(), to.getId(), request.amount());
        }

        Transfer transfer = new Transfer();
        transfer.setTransferRef(transferRef);
        transfer.setFromAccount(request.fromAccount());
        transfer.setToAccount(request.toAccount());
        transfer.setAmount(request.amount());
        transfer.setStatus(status);
        transfer.setOrderId(request.orderId());
        transferRepository.save(transfer);

        if ("COMPLETED".equals(status)) {
            eventPublisher.publish(new TransferEvent(
                    transferRef, request.fromAccount(), request.toAccount(),
                    request.amount(), request.orderId(), status));
        }

        return new TransferResponse(transferRef, status);
    }

    @Transactional(readOnly = true)
    public TransferResponse getByRef(String transferRef) {
        Transfer transfer = transferRepository.findByTransferRef(transferRef)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Transfer not found: " + transferRef));
        return new TransferResponse(transfer.getTransferRef(), transfer.getStatus());
    }
}
