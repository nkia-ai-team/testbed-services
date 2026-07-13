package com.commerce.payment.service;

import com.commerce.common.dto.PaymentRequest;
import com.commerce.common.dto.PaymentResponse;
import com.commerce.payment.client.BankingTransferClient;
import com.commerce.payment.client.BankingTransferResponse;
import com.commerce.payment.client.PgApiClient;
import com.commerce.payment.entity.Payment;
import com.commerce.payment.entity.PaymentLog;
import com.commerce.payment.repository.PaymentLogRepository;
import com.commerce.payment.repository.PaymentRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.Map;

@Service
public class PaymentService {

    private final PaymentRepository paymentRepository;
    private final PaymentLogRepository paymentLogRepository;
    private final PgApiClient pgApiClient;
    private final BankingTransferClient bankingTransferClient;

    public PaymentService(PaymentRepository paymentRepository,
                          PaymentLogRepository paymentLogRepository,
                          PgApiClient pgApiClient,
                          BankingTransferClient bankingTransferClient) {
        this.paymentRepository = paymentRepository;
        this.paymentLogRepository = paymentLogRepository;
        this.pgApiClient = pgApiClient;
        this.bankingTransferClient = bankingTransferClient;
    }

    @Transactional
    public PaymentResponse processPayment(PaymentRequest request) {
        Payment payment = new Payment();
        payment.setOrderId(request.orderId());
        payment.setAmount(request.amount());
        payment.setMethod(request.method() != null ? request.method() : "CARD");
        paymentRepository.save(payment);

        logAction(payment, "PG_REQUEST", "Requesting payment to PG API");

        Map<String, Object> pgResponse = pgApiClient.requestPayment(
                request.orderId(), request.amount(), payment.getMethod());

        String transactionId = (String) pgResponse.get("transaction_id");
        String pgStatus = (String) pgResponse.get("status");

        payment.setStatus(pgStatus);
        payment.setPgTransactionId(transactionId);
        paymentRepository.save(payment);

        logAction(payment, "PG_RESPONSE", "PG response: " + pgStatus + ", txId: " + transactionId);

        // cross-domain: 결제 정산을 core-banking 이체로 위임한다(같은 trace 로 이어짐).
        logAction(payment, "BANKING_TRANSFER_REQUEST", "Requesting settlement transfer to core-banking");
        BankingTransferResponse transfer = bankingTransferClient.transfer(
                request.orderId(), request.amount());
        logAction(payment, "BANKING_TRANSFER_RESPONSE",
                "Banking transfer: " + (transfer != null ? transfer.status() : "no-response"));

        return new PaymentResponse(payment.getId(), payment.getStatus(), transactionId);
    }

    private void logAction(Payment payment, String action, String detail) {
        PaymentLog log = new PaymentLog();
        log.setPayment(payment);
        log.setAction(action);
        log.setDetail(detail);
        paymentLogRepository.save(log);
    }
}
