package com.corebanking.common.dto;

import java.math.BigDecimal;

/**
 * transfer-service 가 Redis Stream("transfer-events") 으로 발행하고
 * ledger-service consumer group 이 소비하는 이벤트.
 */
public record TransferEvent(
        String transferId,
        String fromAccount,
        String toAccount,
        BigDecimal amount,
        String orderId,
        String status
) {
}
