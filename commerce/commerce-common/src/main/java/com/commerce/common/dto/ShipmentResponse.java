package com.commerce.common.dto;

import java.time.LocalDateTime;

public record ShipmentResponse(
        Long shipmentId,
        Long orderId,
        String status,
        String trackingNumber,
        String carrier,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {}
