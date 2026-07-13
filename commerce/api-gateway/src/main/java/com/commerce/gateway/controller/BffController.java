package com.commerce.gateway.controller;

import com.commerce.common.dto.UserOverviewResponse;
import com.commerce.gateway.service.AggregationService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/bff")
public class BffController {

    private final AggregationService aggregationService;

    public BffController(AggregationService aggregationService) {
        this.aggregationService = aggregationService;
    }

    @GetMapping("/users/{userId}/overview")
    public ResponseEntity<UserOverviewResponse> overview(@PathVariable Long userId) {
        return ResponseEntity.ok(aggregationService.getOverview(userId));
    }
}
