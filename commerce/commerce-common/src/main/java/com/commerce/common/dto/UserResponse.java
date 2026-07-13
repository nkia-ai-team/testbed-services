package com.commerce.common.dto;

import java.time.LocalDateTime;

public record UserResponse(
        Long userId,
        String email,
        String name,
        String phone,
        LocalDateTime createdAt
) {}
