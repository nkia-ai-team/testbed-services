package com.commerce.common.dto;

public record CheckoutRequest(
        Long userId,
        String couponCode
) {}
