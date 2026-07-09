package com.corebanking.common.dto;

import java.math.BigDecimal;

/**
 * cross-domain 고정 인터페이스 요청 DTO.
 * commerce-payment → core-banking-transfer (POST /api/transfers) 가 이 형태로 호출한다.
 */
public record TransferRequest(
        String fromAccount,
        String toAccount,
        BigDecimal amount,
        String orderId
) {
}
