package com.nkia.socialfeed.feed.controller;

import com.nkia.socialfeed.common.dto.FeedEntryResponse;
import com.nkia.socialfeed.feed.service.FeedService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
public class FeedController {

    private final FeedService feedService;

    public FeedController(FeedService feedService) {
        this.feedService = feedService;
    }

    @GetMapping("/api/feed/{userId}")
    public ResponseEntity<List<FeedEntryResponse>> getFeed(@PathVariable Long userId) {
        return ResponseEntity.ok(feedService.getFeed(userId));
    }
}
