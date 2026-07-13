package com.commerce.common.dto;

public record CartItemRequest(
        Long productId,
        int quantity
) {}
