package com.commerce.payment.controller;

import com.commerce.common.dto.PaymentRequest;
import com.commerce.common.dto.PaymentResponse;
import com.commerce.common.dto.PaymentSummaryResponse;
import com.commerce.common.dto.SettlementSummaryResponse;
import com.commerce.payment.service.PaymentService;
import org.springframework.data.domain.Pageable;
import org.springframework.data.web.PageableDefault;
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

    @PostMapping
    public ResponseEntity<PaymentResponse> processPayment(@RequestBody PaymentRequest request) {
        return ResponseEntity.ok(paymentService.processPayment(request));
    }

    // §7 신규 — 기존에 GET 목록 엔드포인트가 없었으므로 하위호환 이슈 없음.
    @GetMapping
    public ResponseEntity<List<PaymentSummaryResponse>> list(
            @RequestParam(required = false) String status,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime from,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime to,
            @PageableDefault(size = 20) Pageable pageable) {
        return ResponseEntity.ok(paymentService.list(status, from, to, pageable));
    }

    // 4번 증분 settlement 배치가 쌓는 settlement_summary 노출.
    @GetMapping("/settlements")
    public ResponseEntity<List<SettlementSummaryResponse>> settlements(
            @PageableDefault(size = 20) Pageable pageable) {
        return ResponseEntity.ok(paymentService.getSettlements(pageable));
    }
}
