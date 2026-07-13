package com.commerce.common.dto;

public record InventoryListItemResponse(
        Long productId,
        int stock,
        int reserved,
        boolean available
) {}
