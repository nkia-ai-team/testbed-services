package com.commerce.pricing.controller;

import com.commerce.common.dto.*;
import com.commerce.pricing.service.PricingService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/pricing")
public class PricingController {

    private final PricingService pricingService;

    public PricingController(PricingService pricingService) {
        this.pricingService = pricingService;
    }

    @GetMapping("/products/{productId}")
    public ResponseEntity<PriceResponse> getPrice(@PathVariable Long productId) {
        return ResponseEntity.ok(pricingService.getPrice(productId));
    }

    @PostMapping("/quote")
    public ResponseEntity<QuoteResponse> calculateQuote(@RequestBody QuoteRequest request) {
        return ResponseEntity.ok(pricingService.calculateQuote(request));
    }

    @GetMapping("/promotions")
    public ResponseEntity<List<PromotionResponse>> getPromotions() {
        return ResponseEntity.ok(pricingService.getPromotions());
    }

    @GetMapping("/coupons")
    public ResponseEntity<List<CouponResponse>> getCoupons() {
        return ResponseEntity.ok(pricingService.getCoupons());
    }
}
