package com.fooddelivery.common.dto;

import java.time.LocalDateTime;

public record DispatchResponse(
        Long id,
        Long orderId,
        String courierId,
        Integer etaMinutes,
        String status,
        LocalDateTime assignedAt
) {}
