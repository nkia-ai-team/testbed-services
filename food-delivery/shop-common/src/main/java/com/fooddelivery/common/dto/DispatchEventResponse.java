package com.fooddelivery.common.dto;

import java.time.LocalDateTime;

public record DispatchEventResponse(
        Long id,
        Long dispatchId,
        String status,
        LocalDateTime occurredAt
) {}
