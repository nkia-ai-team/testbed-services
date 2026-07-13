package com.commerce.product.controller;

import com.commerce.common.dto.ProductResponse;
import com.commerce.common.dto.ProductVariantResponse;
import com.commerce.common.dto.ReserveStockRequest;
import com.commerce.common.dto.ReserveStockResponse;
import com.commerce.product.service.ProductService;
import org.springframework.data.domain.Pageable;
import org.springframework.data.web.PageableDefault;
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

    // category/q가 §7 신규 파라미터명, categoryId/name은 기존 호출자(loadgen, 시나리오 등)를
    // 위한 하위호환 별칭 — 둘 다 지원하고 신규 이름이 있으면 우선한다.
    @GetMapping
    public ResponseEntity<List<ProductResponse>> getProducts(
            @RequestParam(required = false) Long category,
            @RequestParam(required = false) Long categoryId,
            @RequestParam(required = false) String q,
            @RequestParam(required = false) String name,
            @PageableDefault(size = 20) Pageable pageable) {
        Long resolvedCategory = category != null ? category : categoryId;
        String resolvedKeyword = q != null ? q : name;
        return ResponseEntity.ok(productService.getProducts(resolvedCategory, resolvedKeyword, pageable));
    }

    @GetMapping("/{id}")
    public ResponseEntity<ProductResponse> getProduct(@PathVariable Long id) {
        return ResponseEntity.ok(productService.getProductWithStock(id));
    }

    @GetMapping("/{id}/variants")
    public ResponseEntity<List<ProductVariantResponse>> getVariants(@PathVariable Long id) {
        return ResponseEntity.ok(productService.getVariants(id));
    }

    @PostMapping("/{id}/reserve-stock")
    public ResponseEntity<ReserveStockResponse> reserveStock(@PathVariable Long id,
                                                              @RequestBody ReserveStockRequest request) {
        return ResponseEntity.ok(productService.reserveStock(id, request.quantity()));
    }
}
