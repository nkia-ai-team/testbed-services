package com.corebanking.common.dto;

import java.math.BigDecimal;

public record AccountResponse(
        String id,
        String holder,
        BigDecimal balance,
        String status
) {
}
