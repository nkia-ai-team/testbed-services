package com.fooddelivery.payment.controller;

import com.fooddelivery.common.dto.PaymentRequest;
import com.fooddelivery.common.dto.PaymentResponse;
import com.fooddelivery.common.dto.SettlementSummaryResponse;
import com.fooddelivery.payment.service.PaymentService;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.PageRequest;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.time.LocalDateTime;
import java.util.List;

@RestController
@RequestMapping("/api/payments")
public class PaymentController {

    private final PaymentService paymentService;

    public PaymentController(PaymentService paymentService) {
        this.paymentService = paymentService;
    }

    @PostMapping("")
    public ResponseEntity<PaymentResponse> processPayment(@RequestBody PaymentRequest request) {
        return ResponseEntity.ok(paymentService.processPayment(request));
    }

    // §7 신규 — status/from/to/page/size. 전부 없으면 무제한 목록 조회.
    @GetMapping
    public ResponseEntity<List<PaymentResponse>> getPayments(
            @RequestParam(required = false) String status,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime from,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime to,
            @RequestParam(required = false) Integer page,
            @RequestParam(required = false) Integer size) {
        Pageable pageable = (page != null || size != null)
                ? PageRequest.of(page != null ? page : 0, size != null ? size : 20)
                : Pageable.unpaged();
        return ResponseEntity.ok(paymentService.searchPayments(status, from, to, pageable));
    }

    // §7 신규 — SettlementBatch 정산 요약 목록.
    @GetMapping("/settlements")
    public ResponseEntity<List<SettlementSummaryResponse>> getSettlements(
            @RequestParam(required = false) Integer page,
            @RequestParam(required = false) Integer size) {
        Pageable pageable = (page != null || size != null)
                ? PageRequest.of(page != null ? page : 0, size != null ? size : 20)
                : Pageable.unpaged();
        return ResponseEntity.ok(paymentService.getSettlements(pageable));
    }
}
