package com.nkia.socialfeed.comment.service;

import com.nkia.socialfeed.comment.entity.Comment;
import com.nkia.socialfeed.comment.repository.CommentRepository;
import com.nkia.socialfeed.common.dto.CommentRequest;
import com.nkia.socialfeed.common.dto.CommentResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

@Service
public class CommentService {

    private static final Logger log = LoggerFactory.getLogger(CommentService.class);

    private final CommentRepository commentRepository;

    public CommentService(CommentRepository commentRepository) {
        this.commentRepository = commentRepository;
    }

    @Transactional
    public CommentResponse createComment(Long postId, CommentRequest request) {
        Comment comment = new Comment();
        comment.setPostId(postId);
        comment.setAuthorId(request.authorId());
        comment.setContent(request.content());
        commentRepository.save(comment);
        log.info("Comment created id={} on post={}", comment.getId(), postId);
        return toResponse(comment);
    }

    @Transactional(readOnly = true)
    public List<CommentResponse> getCommentsByPost(Long postId) {
        return commentRepository.findByPostIdOrderByIdAsc(postId).stream()
                .map(this::toResponse)
                .toList();
    }

    private CommentResponse toResponse(Comment c) {
        return new CommentResponse(
                c.getId(),
                c.getPostId(),
                c.getAuthorId(),
                c.getContent(),
                c.getCreatedAt()
        );
    }
}
