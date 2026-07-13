package com.fooddelivery.common.dto;

public record DispatchEvent(
        Long dispatchId,
        Long orderId,
        String status,
        String eventType
) {}
