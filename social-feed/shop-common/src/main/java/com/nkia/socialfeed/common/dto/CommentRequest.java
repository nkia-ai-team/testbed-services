package com.nkia.socialfeed.common.dto;

public record CommentRequest(
        Long authorId,
        String content
) {
}
