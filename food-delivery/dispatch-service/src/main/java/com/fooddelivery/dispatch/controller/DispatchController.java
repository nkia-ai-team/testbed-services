package com.fooddelivery.dispatch.controller;

import com.fooddelivery.common.dto.DispatchRequest;
import com.fooddelivery.common.dto.DispatchResponse;
import com.fooddelivery.dispatch.service.DispatchService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

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

    @GetMapping("/{id}")
    public ResponseEntity<DispatchResponse> getDispatch(@PathVariable Long id) {
        return ResponseEntity.ok(dispatchService.getDispatch(id));
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
