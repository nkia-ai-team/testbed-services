package com.plopvape.payment.client;

/**
 * cross-domain 응답 DTO: core-banking-transfer → commerce-payment.
 * status 는 "COMPLETED" 또는 "FAILED".
 */
public record BankingTransferResponse(
        String transferId,
        String status
) {}
