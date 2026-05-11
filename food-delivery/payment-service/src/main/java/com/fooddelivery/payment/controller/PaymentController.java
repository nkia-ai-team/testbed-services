package com.fooddelivery.payment.controller;

import com.fooddelivery.common.dto.PaymentRequest;
import com.fooddelivery.common.dto.PaymentResponse;
import com.fooddelivery.payment.service.PaymentService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/payments")
public class PaymentController {

    private final PaymentService paymentService;

    public PaymentController(PaymentService paymentService) {
        this.paymentService = paymentService;
    }

    // WPM Spring scanner consistency — value 명시 (memory note infra_polestar_wpm_k8s_limits.md).
    @PostMapping("")
    public ResponseEntity<PaymentResponse> processPayment(@RequestBody PaymentRequest request) {
        return ResponseEntity.ok(paymentService.processPayment(request));
    }
}
