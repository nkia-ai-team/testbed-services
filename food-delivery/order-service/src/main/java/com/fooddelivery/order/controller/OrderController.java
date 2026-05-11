package com.fooddelivery.order.controller;

import com.fooddelivery.common.dto.OrderRequest;
import com.fooddelivery.common.dto.OrderResponse;
import com.fooddelivery.order.service.OrderService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    // WPM Spring scanner 가 value 없는 @PostMapping 을 인식 못함 (memory note infra_polestar_wpm_k8s_limits.md).
    // 같은 controller 의 다른 method 가 value 있을 때 전체 controller scan 이 broken — value 명시로 회피.
    @PostMapping("")
    public ResponseEntity<OrderResponse> createOrder(@RequestBody OrderRequest request) {
        return ResponseEntity.ok(orderService.createOrder(request));
    }

    @GetMapping("/{id}")
    public ResponseEntity<OrderResponse> getOrder(@PathVariable Long id) {
        return ResponseEntity.ok(orderService.getOrder(id));
    }
}
