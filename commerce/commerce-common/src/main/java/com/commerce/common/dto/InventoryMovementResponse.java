package com.commerce.common.dto;

import java.time.LocalDateTime;

public record InventoryMovementResponse(
        Long id,
        Long productId,
        String movementType,
        int quantity,
        int resultingStock,
        LocalDateTime createdAt
) {}
