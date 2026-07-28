package com.commerce.order.controller;

import com.commerce.common.dto.CheckoutRequest;
import com.commerce.common.dto.DailyOrderStatResponse;
import com.commerce.common.dto.OrderRequest;
import com.commerce.common.dto.OrderResponse;
import com.commerce.order.service.OrderReportRenderer;
import com.commerce.order.service.OrderService;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.PageRequest;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    private final OrderService orderService;
    private final OrderReportRenderer reportRenderer;

    public OrderController(OrderService orderService, OrderReportRenderer reportRenderer) {
        this.orderService = orderService;
        this.reportRenderer = reportRenderer;
    }

    // 기존 API — 시나리오 러너가 items를 직접 지정해 호출하므로 그대로 유지한다.
    @PostMapping
    public ResponseEntity<OrderResponse> createOrder(@RequestBody OrderRequest request) {
        return ResponseEntity.ok(orderService.createOrder(request));
    }

    // 신규 checkout 오케스트레이션 — cart→pricing→inventory→payment 5-hop.
    @PostMapping("/checkout")
    public ResponseEntity<OrderResponse> checkout(@RequestBody CheckoutRequest request) {
        return ResponseEntity.ok(orderService.checkout(request));
    }

    // §7 확장: status/from/to/page/size 추가. userId만 넘기던 기존 호출(BFF 포함)은 그대로
    // 동작한다 — 응답은 여전히 배열이다(하위호환, OrderService.searchOrders 주석 참조).
    // page/size를 직접 nullable Integer로 받는 이유: @PageableDefault를 쓰면 파라미터를
    // 하나도 안 넘겨도 Pageable이 "paged" 상태(0,20)로 resolve되어, 원래 무제한이던 응답이
    // 조용히 20건으로 잘리는 회귀가 생긴다 — page/size를 실제로 넘겼을 때만 페이지네이션을 켠다.
    @GetMapping
    public ResponseEntity<List<OrderResponse>> getAllOrders(
            @RequestParam(required = false) Long userId,
            @RequestParam(required = false) String status,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime from,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime to,
            @RequestParam(required = false) Integer page,
            @RequestParam(required = false) Integer size) {
        if (status == null && from == null && to == null && page == null && size == null) {
            // 기존 경로(파라미터 없음 또는 userId만) — 원래 메서드를 그대로 사용해 동작을 100% 보존.
            return ResponseEntity.ok(orderService.getAllOrders(userId));
        }
        Pageable pageable = (page != null || size != null)
                ? PageRequest.of(page != null ? page : 0, size != null ? size : 20)
                : Pageable.unpaged();
        return ResponseEntity.ok(orderService.searchOrders(userId, status, from, to, pageable));
    }

    /**
     * 주문 리포트 렌더링. 요청 스레드에서 동기로 렌더한다.
     *
     * <p>{@link OrderReportRenderer}는 내부 버퍼를 재사용하는 단일 인스턴스라
     * 렌더가 직렬화되고, 렌더 비용이 구간 길이의 제곱으로 늘어난다. 구간이 짧을 때는
     * 문제가 드러나지 않지만, 긴 구간 요청이 동시에 들어오면 처리율 상한을 넘겨
     * 대기 요청이 서블릿 워커를 쥔 채 쌓인다.
     */
    @GetMapping("/reports/render")
    public ResponseEntity<Map<String, Object>> renderReport(
            @RequestParam(defaultValue = "30") int days) {
        int bounded = Math.min(Math.max(days, 1), 365);
        String report = reportRenderer.render(bounded);
        return ResponseEntity.ok(Map.of(
                "status", "RENDERED",
                "days", bounded,
                "lines", report.lines().count()));
    }

    // 챗봇 추세/집계 질의 재료 — 최근 days일 일별 주문수·금액.
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
