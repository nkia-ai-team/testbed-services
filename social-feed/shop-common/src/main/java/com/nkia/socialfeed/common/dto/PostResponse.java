package com.nkia.socialfeed.common.dto;

import java.time.LocalDateTime;

public record PostResponse(
        Long id,
        Long authorId,
        String content,
        LocalDateTime createdAt
) {
}
