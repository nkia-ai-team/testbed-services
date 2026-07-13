package com.commerce.common.dto;

public record LoginRequest(
        String email,
        String password
) {}
