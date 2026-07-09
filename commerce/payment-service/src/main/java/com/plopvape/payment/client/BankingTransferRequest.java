package com.plopvape.payment.client;

import java.math.BigDecimal;

/**
 * cross-domain 요청 DTO: commerce-payment → core-banking-transfer.
 * 고정 인터페이스(POST /api/transfers) 계약에 맞춘 형태.
 */
public record BankingTransferRequest(
        String fromAccount,
        String toAccount,
        BigDecimal amount,
        String orderId
) {}
