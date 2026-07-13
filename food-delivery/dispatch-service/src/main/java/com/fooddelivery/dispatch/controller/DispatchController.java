package com.fooddelivery.dispatch.controller;

import com.fooddelivery.common.dto.DispatchEventResponse;
import com.fooddelivery.common.dto.DispatchRequest;
import com.fooddelivery.common.dto.DispatchResponse;
import com.fooddelivery.dispatch.service.DispatchService;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.PageRequest;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/deliveries")
public class DispatchController {

    private final DispatchService dispatchService;

    public DispatchController(DispatchService dispatchService) {
        this.dispatchService = dispatchService;
    }

    @PostMapping("/dispatch")
    public ResponseEntity<DispatchResponse> dispatchCourier(@RequestBody DispatchRequest request) {
        return ResponseEntity.ok(dispatchService.dispatchCourier(request));
    }

    // §7 신규 — status/page/size. 둘 다 없으면 무제한.
    @GetMapping
    public ResponseEntity<List<DispatchResponse>> getDeliveries(
            @RequestParam(required = false) String status,
            @RequestParam(required = false) Integer page,
            @RequestParam(required = false) Integer size) {
        Pageable pageable = (page != null || size != null)
                ? PageRequest.of(page != null ? page : 0, size != null ? size : 20)
                : Pageable.unpaged();
        return ResponseEntity.ok(dispatchService.searchDispatches(status, pageable));
    }

    @GetMapping("/{id}")
    public ResponseEntity<DispatchResponse> getDispatch(@PathVariable Long id) {
        return ResponseEntity.ok(dispatchService.getDispatch(id));
    }

    // §7 신규 — 배차 상태 전이 이력.
    @GetMapping("/{id}/events")
    public ResponseEntity<List<DispatchEventResponse>> getEvents(
            @PathVariable Long id,
            @RequestParam(required = false) Integer page,
            @RequestParam(required = false) Integer size) {
        Pageable pageable = (page != null || size != null)
                ? PageRequest.of(page != null ? page : 0, size != null ? size : 20)
                : Pageable.unpaged();
        return ResponseEntity.ok(dispatchService.getEvents(id, pageable));
    }

    /**
     * 가용 courier capacity 조회. order-service 가 주문 생성 전 호출.
     * 응답: {"currentAssigned": int, "maxCapacity": int, "available": int}
     */
    @GetMapping("/capacity")
    public ResponseEntity<Map<String, Integer>> getCapacity() {
        return ResponseEntity.ok(dispatchService.getCapacity());
    }
}
