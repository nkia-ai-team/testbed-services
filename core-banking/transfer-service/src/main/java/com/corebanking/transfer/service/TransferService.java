package com.corebanking.transfer.service;

import com.corebanking.common.dto.TransferEvent;
import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import com.corebanking.common.exception.ServiceException;
import com.corebanking.transfer.dto.DailyTransferStatResponse;
import com.corebanking.transfer.dto.TransferSummaryResponse;
import com.corebanking.transfer.entity.Account;
import com.corebanking.transfer.entity.Transfer;
import com.corebanking.transfer.event.TransferEventPublisher;
import com.corebanking.transfer.repository.AccountRepository;
import com.corebanking.transfer.repository.TransferRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.domain.Pageable;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.sql.Timestamp;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.util.List;
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
     * 잔액 차감/증가 + transfers row 기록 후 outbox_events 에 이벤트를 기록한다
     * (Kafka banking.transfers 로는 OutboxRelay 가 비동기 발행).
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

    /**
     * 이체 목록 조회(계좌/상태/기간 필터 + 선택적 페이지네이션).
     * page/size 둘 다 미지정이면 unpaged 로 조회(기존 무제한 조회 관례 유지).
     */
    @Transactional(readOnly = true)
    public List<TransferSummaryResponse> search(String fromAccount, String toAccount, String status,
                                                  LocalDateTime from, LocalDateTime to, Pageable pageable) {
        return transferRepository.search(fromAccount, toAccount, status, from, to, pageable)
                .map(this::toSummary)
                .getContent();
    }

    @Transactional(readOnly = true)
    public List<DailyTransferStatResponse> dailyStats(int days) {
        LocalDateTime since = LocalDate.now().minusDays(days).atStartOfDay();
        return transferRepository.dailyStatsSince(since).stream()
                .map(row -> new DailyTransferStatResponse(
                        toLocalDate(row[0]),
                        ((Number) row[1]).longValue(),
                        (BigDecimal) row[2]))
                .toList();
    }

    private LocalDate toLocalDate(Object value) {
        if (value instanceof Timestamp ts) {
            return ts.toLocalDateTime().toLocalDate();
        }
        if (value instanceof java.sql.Date d) {
            return d.toLocalDate();
        }
        if (value instanceof LocalDateTime ldt) {
            return ldt.toLocalDate();
        }
        if (value instanceof LocalDate ld) {
            return ld;
        }
        throw new IllegalStateException("Unexpected date type from query: " + value.getClass());
    }

    private TransferSummaryResponse toSummary(Transfer t) {
        return new TransferSummaryResponse(t.getId(), t.getTransferRef(), t.getFromAccount(),
                t.getToAccount(), t.getAmount(), t.getStatus(), t.getOrderId(), t.getCreatedAt());
    }

    /**
     * 오래된 PENDING 이체 정리 배치용. 정상 흐름에서는 execute() 가 즉시 COMPLETED/FAILED 로
     * 확정하므로 PENDING 이 남는 경우는 비정상(예: 예외로 커밋 실패)뿐이다 — 안전하게 FAILED 로 종료 처리.
     */
    @Transactional
    public int cleanupStalePending(int staleAfterMinutes) {
        LocalDateTime cutoff = LocalDateTime.now().minusMinutes(staleAfterMinutes);
        List<Transfer> stale = transferRepository.findByStatusAndCreatedAtBefore("PENDING", cutoff);
        for (Transfer t : stale) {
            t.setStatus("FAILED");
            transferRepository.save(t);
        }
        return stale.size();
    }
}
