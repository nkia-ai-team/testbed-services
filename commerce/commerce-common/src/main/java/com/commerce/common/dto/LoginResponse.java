package com.commerce.common.dto;

import java.time.LocalDateTime;

public record LoginResponse(
        String token,
        Long userId,
        LocalDateTime expiresAt
) {}
