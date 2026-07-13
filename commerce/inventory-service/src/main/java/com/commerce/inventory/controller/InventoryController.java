package com.commerce.inventory.controller;

import com.commerce.common.dto.InventoryListItemResponse;
import com.commerce.common.dto.InventoryMovementResponse;
import com.commerce.common.dto.InventoryReleaseRequest;
import com.commerce.common.dto.InventoryReserveRequest;
import com.commerce.common.dto.InventoryReserveResponse;
import com.commerce.inventory.service.InventoryService;
import org.springframework.data.domain.Pageable;
import org.springframework.data.web.PageableDefault;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/inventory")
public class InventoryController {

    private final InventoryService inventoryService;

    public InventoryController(InventoryService inventoryService) {
        this.inventoryService = inventoryService;
    }

    // §7 신규 목록 — 기존 /api/inventory/{productId}(단건 조회) 등 4개 엔드포인트는 그대로 유지.
    @GetMapping
    public ResponseEntity<List<InventoryListItemResponse>> list(
            @RequestParam(defaultValue = "false") boolean lowStockOnly,
            @PageableDefault(size = 20) Pageable pageable) {
        return ResponseEntity.ok(inventoryService.list(lowStockOnly, pageable));
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

    // §7 신규 — 4번 증분에서 쌓기 시작한 inventory_movements 원장 노출.
    @GetMapping("/{productId}/movements")
    public ResponseEntity<List<InventoryMovementResponse>> getMovements(
            @PathVariable Long productId,
            @PageableDefault(size = 20) Pageable pageable) {
        return ResponseEntity.ok(inventoryService.getMovements(productId, pageable));
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
