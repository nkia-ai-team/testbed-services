package com.commerce.common.dto;

import java.time.LocalDateTime;

public record ShipmentEventResponse(
        Long id,
        Long shipmentId,
        String status,
        LocalDateTime occurredAt
) {}
