package com.nkia.socialfeed.comment.controller;

import com.nkia.socialfeed.comment.service.CommentService;
import com.nkia.socialfeed.common.dto.CommentRequest;
import com.nkia.socialfeed.common.dto.CommentResponse;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/posts/{id}/comments")
public class CommentController {

    private final CommentService commentService;

    public CommentController(CommentService commentService) {
        this.commentService = commentService;
    }

    @PostMapping
    public ResponseEntity<CommentResponse> createComment(@PathVariable("id") Long postId,
                                                         @RequestBody CommentRequest request) {
        return ResponseEntity.ok(commentService.createComment(postId, request));
    }

    @GetMapping
    public ResponseEntity<List<CommentResponse>> getComments(@PathVariable("id") Long postId) {
        return ResponseEntity.ok(commentService.getCommentsByPost(postId));
    }
}
