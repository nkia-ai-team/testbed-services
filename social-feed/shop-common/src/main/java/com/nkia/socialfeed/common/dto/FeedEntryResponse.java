package com.nkia.socialfeed.common.dto;

import java.time.LocalDateTime;

public record FeedEntryResponse(
        Long id,
        Long userId,
        Long postId,
        String content,
        Long authorId,
        LocalDateTime createdAt
) {
}
