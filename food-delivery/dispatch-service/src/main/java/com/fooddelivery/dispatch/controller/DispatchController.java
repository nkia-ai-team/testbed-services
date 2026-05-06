package com.fooddelivery.dispatch.controller;

import com.fooddelivery.common.dto.DispatchRequest;
import com.fooddelivery.common.dto.DispatchResponse;
import com.fooddelivery.dispatch.service.DispatchService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

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
}
