package com.commerce.common.dto;

import java.time.LocalDateTime;
import java.util.List;

public record CartResponse(
        Long userId,
        List<CartItemResponse> items,
        LocalDateTime updatedAt
) {}
