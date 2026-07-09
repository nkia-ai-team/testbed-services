package com.corebanking.api.controller;

import com.corebanking.api.service.ApiService;
import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import org.springframework.http.ResponseEntity;
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
}
