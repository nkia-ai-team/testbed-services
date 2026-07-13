package com.commerce.common.dto;

public record InventoryReserveRequest(
        Long productId,
        int quantity
) {}
