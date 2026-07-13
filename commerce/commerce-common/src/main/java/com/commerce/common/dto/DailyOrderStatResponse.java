package com.commerce.common.dto;

import java.math.BigDecimal;
import java.time.LocalDate;

public record DailyOrderStatResponse(
        LocalDate date,
        long orderCount,
        BigDecimal totalAmount
) {}
