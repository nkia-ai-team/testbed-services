package com.plopvape.inventory.controller;

import com.plopvape.common.dto.InventoryReleaseRequest;
import com.plopvape.common.dto.InventoryReserveRequest;
import com.plopvape.common.dto.InventoryReserveResponse;
import com.plopvape.inventory.service.InventoryService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@RequestMapping("/api/inventory")
public class InventoryController {

    private final InventoryService inventoryService;

    public InventoryController(InventoryService inventoryService) {
        this.inventoryService = inventoryService;
    }

    @GetMapping("/{productId}")
    public ResponseEntity<Map<String, Object>> getStock(@PathVariable Long productId) {
        var inventory = inventoryService.getByProductId(productId);
        return ResponseEntity.ok(Map.of(
                "productId", inventory.getProductId(),
                "stock", inventory.getStock(),
                "available", inventory.getStock() > 0
        ));
    }

    @PostMapping("/reserve")
    public ResponseEntity<InventoryReserveResponse> reserve(@RequestBody InventoryReserveRequest request) {
        return ResponseEntity.ok(inventoryService.reserve(request));
    }

    @PostMapping("/release")
    public ResponseEntity<Map<String, Object>> release(@RequestBody InventoryReleaseRequest request) {
        var inventory = inventoryService.release(request);
        return ResponseEntity.ok(Map.of(
                "productId", inventory.getProductId(),
                "released", true,
                "currentStock", inventory.getStock()
        ));
    }

    @PutMapping("/{productId}")
    public ResponseEntity<Map<String, Object>> updateStock(@PathVariable Long productId,
                                                           @RequestBody Map<String, Integer> body) {
        var inventory = inventoryService.updateStock(productId, body.get("stock"));
        return ResponseEntity.ok(Map.of(
                "productId", inventory.getProductId(),
                "stock", inventory.getStock()
        ));
    }
}
