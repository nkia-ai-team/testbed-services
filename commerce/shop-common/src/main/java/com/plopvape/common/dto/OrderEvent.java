package com.plopvape.common.dto;

import java.math.BigDecimal;

public record OrderEvent(
        Long orderId,
        String customerName,
        String customerEmail,
        BigDecimal totalAmount,
        String status
) {}
