package com.commerce.product.controller;

import com.commerce.common.dto.ProductResponse;
import com.commerce.common.dto.ReserveStockRequest;
import com.commerce.common.dto.ReserveStockResponse;
import com.commerce.product.service.ProductService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/products")
public class ProductController {

    private final ProductService productService;

    public ProductController(ProductService productService) {
        this.productService = productService;
    }

    @GetMapping
    public ResponseEntity<List<ProductResponse>> getProducts(
            @RequestParam(required = false) Long categoryId,
            @RequestParam(required = false) String name) {
        return ResponseEntity.ok(productService.getProducts(categoryId, name));
    }

    @GetMapping("/{id}")
    public ResponseEntity<ProductResponse> getProduct(@PathVariable Long id) {
        return ResponseEntity.ok(productService.getProductWithStock(id));
    }

    @PostMapping("/{id}/reserve-stock")
    public ResponseEntity<ReserveStockResponse> reserveStock(@PathVariable Long id,
                                                              @RequestBody ReserveStockRequest request) {
        return ResponseEntity.ok(productService.reserveStock(id, request.quantity()));
    }
}
