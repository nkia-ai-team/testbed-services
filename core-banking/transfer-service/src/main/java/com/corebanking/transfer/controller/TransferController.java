package com.corebanking.transfer.controller;

import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import com.corebanking.transfer.service.TransferService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

/**
 * cross-domain 고정 인터페이스.
 *   POST /api/transfers  { fromAccount, toAccount, amount, orderId }
 *     -> { transferId, status: COMPLETED|FAILED }
 * commerce-payment 가 FQDN(transfer-service.rca-testbed-banking...) 으로 이 API 를 호출한다.
 * traceparent 는 OTel javaagent 가 자동 전파.
 */
@RestController
@RequestMapping("/api/transfers")
public class TransferController {

    private final TransferService transferService;

    public TransferController(TransferService transferService) {
        this.transferService = transferService;
    }

    // WPM Spring scanner 가 value 없는 @PostMapping 을 인식 못함 → value 명시로 회피.
    @PostMapping("")
    public ResponseEntity<TransferResponse> transfer(@RequestBody TransferRequest request) {
        return ResponseEntity.ok(transferService.execute(request));
    }

    @GetMapping("/{transferRef}")
    public ResponseEntity<TransferResponse> get(@PathVariable String transferRef) {
        return ResponseEntity.ok(transferService.getByRef(transferRef));
    }
}
