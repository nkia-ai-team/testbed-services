package com.nkia.socialfeed.common.dto;

import java.time.LocalDateTime;

public record CommentResponse(
        Long id,
        Long postId,
        Long authorId,
        String content,
        LocalDateTime createdAt
) {
}
