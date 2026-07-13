package com.corebanking.transfer.controller;

import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import com.corebanking.transfer.dto.DailyTransferStatResponse;
import com.corebanking.transfer.dto.TransferSummaryResponse;
import com.corebanking.transfer.service.TransferService;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.format.annotation.DateTimeFormat.ISO;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.time.LocalDateTime;
import java.util.List;

/**
 * cross-domain 고정 인터페이스.
 *   POST /api/transfers  { fromAccount, toAccount, amount, orderId }
 *     -> { transferId, status: COMPLETED|FAILED }
 * commerce-payment 가 FQDN(transfer-service.rca-testbed-banking...) 으로 이 API 를 호출한다.
 * traceparent 는 OTel javaagent 가 자동 전파.
 * GET /{transferRef} 도 기존 그대로 유지 — 아래 read API 는 신규 추가(경로 정적 세그먼트라 충돌 없음).
 */
@RestController
@RequestMapping("/api/transfers")
public class TransferController {

    private final TransferService transferService;

    public TransferController(TransferService transferService) {
        this.transferService = transferService;
    }

    @PostMapping("")
    public ResponseEntity<TransferResponse> transfer(@RequestBody TransferRequest request) {
        return ResponseEntity.ok(transferService.execute(request));
    }

    @GetMapping("/{transferRef}")
    public ResponseEntity<TransferResponse> get(@PathVariable String transferRef) {
        return ResponseEntity.ok(transferService.getByRef(transferRef));
    }

    /**
     * 이체 목록 조회(계좌/상태/기간 필터 + 선택적 페이지네이션).
     * page/size 둘 다 미지정이면 무제한 조회(@PageableDefault 를 쓰지 않는 이유:
     * 미지정 시에도 (0,20) 으로 묵시 페이징돼 응답이 잘리는 것을 피하기 위함).
     */
    @GetMapping
    public ResponseEntity<List<TransferSummaryResponse>> list(
            @RequestParam(required = false) String fromAccount,
            @RequestParam(required = false) String toAccount,
            @RequestParam(required = false) String status,
            @RequestParam(required = false) @DateTimeFormat(iso = ISO.DATE_TIME) LocalDateTime from,
            @RequestParam(required = false) @DateTimeFormat(iso = ISO.DATE_TIME) LocalDateTime to,
            @RequestParam(required = false) Integer page,
            @RequestParam(required = false) Integer size) {
        Pageable pageable = (page != null || size != null)
                ? PageRequest.of(page != null ? page : 0, size != null ? size : 20)
                : Pageable.unpaged();
        return ResponseEntity.ok(transferService.search(fromAccount, toAccount, status, from, to, pageable));
    }

    @GetMapping("/stats/daily")
    public ResponseEntity<List<DailyTransferStatResponse>> dailyStats(
            @RequestParam(required = false, defaultValue = "30") int days) {
        return ResponseEntity.ok(transferService.dailyStats(days));
    }
}
