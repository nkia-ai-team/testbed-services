package com.commerce.common.dto;

public record InventoryReleaseRequest(
        Long productId,
        int quantity
) {}
