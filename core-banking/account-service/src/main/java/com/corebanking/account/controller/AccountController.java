package com.corebanking.account.controller;

import com.corebanking.account.service.AccountService;
import com.corebanking.common.dto.AccountResponse;
import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/accounts")
public class AccountController {

    private final AccountService accountService;

    public AccountController(AccountService accountService) {
        this.accountService = accountService;
    }

    @GetMapping("/{id}")
    public ResponseEntity<AccountResponse> getAccount(@PathVariable String id) {
        return ResponseEntity.ok(accountService.getAccount(id));
    }

    @PostMapping("/transfer")
    public ResponseEntity<TransferResponse> transfer(@RequestBody TransferRequest request) {
        return ResponseEntity.ok(accountService.validateAndForward(request));
    }
}
