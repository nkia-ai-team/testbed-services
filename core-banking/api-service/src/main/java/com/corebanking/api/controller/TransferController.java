package com.corebanking.api.controller;

import com.corebanking.api.service.ApiService;
import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.util.MultiValueMap;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/transfers")
public class TransferController {

    private final ApiService apiService;

    public TransferController(ApiService apiService) {
        this.apiService = apiService;
    }

    @PostMapping("")
    public ResponseEntity<TransferResponse> submit(@RequestBody TransferRequest request) {
        return ResponseEntity.ok(apiService.submitTransfer(request));
    }

    // 조회 프록시 — 필터/페이지네이션 쿼리 파라미터를 transfer-service에 그대로 전달한다.
    @GetMapping("")
    public ResponseEntity<String> list(@RequestParam MultiValueMap<String, String> params) {
        return ResponseEntity.ok()
                .contentType(MediaType.APPLICATION_JSON)
                .body(apiService.listTransfers(params));
    }
}
