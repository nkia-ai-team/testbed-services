package com.fooddelivery.order.controller;

import com.fooddelivery.common.dto.DailyOrderStatResponse;
import com.fooddelivery.common.dto.OrderRequest;
import com.fooddelivery.common.dto.OrderResponse;
import com.fooddelivery.order.service.OrderService;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.PageRequest;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.time.LocalDateTime;
import java.util.List;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    @PostMapping("")
    public ResponseEntity<OrderResponse> createOrder(@RequestBody OrderRequest request) {
        return ResponseEntity.ok(orderService.createOrder(request));
    }

    // §7 신규 — customerId/status/restaurantId/from/to/page/size. 전부 없으면 무제한 목록 조회.
    @GetMapping
    public ResponseEntity<List<OrderResponse>> getOrders(
            @RequestParam(required = false) String customerId,
            @RequestParam(required = false) String status,
            @RequestParam(required = false) Long restaurantId,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime from,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime to,
            @RequestParam(required = false) Integer page,
            @RequestParam(required = false) Integer size) {
        Pageable pageable = (page != null || size != null)
                ? PageRequest.of(page != null ? page : 0, size != null ? size : 20)
                : Pageable.unpaged();
        return ResponseEntity.ok(orderService.searchOrders(customerId, status, restaurantId, from, to, pageable));
    }

    // §7 신규 — 챗봇 추세/집계 질의 재료.
    @GetMapping("/stats/daily")
    public ResponseEntity<List<DailyOrderStatResponse>> getDailyStats(
            @RequestParam(defaultValue = "30") int days) {
        return ResponseEntity.ok(orderService.getDailyStats(days));
    }

    @GetMapping("/{id}")
    public ResponseEntity<OrderResponse> getOrder(@PathVariable Long id) {
        return ResponseEntity.ok(orderService.getOrder(id));
    }
}
