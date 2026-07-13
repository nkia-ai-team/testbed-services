package com.corebanking.account.controller;

import com.corebanking.account.service.AccountService;
import com.corebanking.common.dto.AccountResponse;
import com.corebanking.common.dto.TransferRequest;
import com.corebanking.common.dto.TransferResponse;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

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

    /**
     * 계좌 목록/검색(상태/예금주 필터 + 선택적 페이지네이션). page/size 미지정 시 무제한 조회.
     */
    @GetMapping
    public ResponseEntity<List<AccountResponse>> list(
            @RequestParam(required = false) String status,
            @RequestParam(required = false) String holder,
            @RequestParam(required = false) Integer page,
            @RequestParam(required = false) Integer size) {
        Pageable pageable = (page != null || size != null)
                ? PageRequest.of(page != null ? page : 0, size != null ? size : 20)
                : Pageable.unpaged();
        return ResponseEntity.ok(accountService.search(status, holder, pageable));
    }

    @PostMapping("/transfer")
    public ResponseEntity<TransferResponse> transfer(@RequestBody TransferRequest request) {
        return ResponseEntity.ok(accountService.validateAndForward(request));
    }
}
