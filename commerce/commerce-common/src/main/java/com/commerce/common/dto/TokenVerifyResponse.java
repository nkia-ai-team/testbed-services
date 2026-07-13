package com.commerce.common.dto;

public record TokenVerifyResponse(
        boolean valid,
        Long userId,
        String email
) {}
