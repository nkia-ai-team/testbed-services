package com.corebanking.ledger.controller;

import com.corebanking.ledger.dto.LedgerEntryResponse;
import com.corebanking.ledger.service.LedgerService;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/ledger-entries")
public class LedgerController {

    private final LedgerService ledgerService;

    public LedgerController(LedgerService ledgerService) {
        this.ledgerService = ledgerService;
    }

    /**
     * 원장 목록 조회. page/size 미지정 시 무제한 조회(기존 관례 유지) —
     * @PageableDefault 를 쓰지 않는 이유는 미지정 시에도 (0,20) 으로 묵시 페이징되어
     * 응답을 20건으로 잘라버리는 것을 피하기 위함(commerce read API 6번 증분과 동일 관례).
     */
    @GetMapping
    public ResponseEntity<List<LedgerEntryResponse>> list(
            @RequestParam(required = false) String accountId,
            @RequestParam(required = false) String transferRef,
            @RequestParam(required = false) String direction,
            @RequestParam(required = false) Integer page,
            @RequestParam(required = false) Integer size) {
        Pageable pageable = (page != null || size != null)
                ? PageRequest.of(page != null ? page : 0, size != null ? size : 20)
                : Pageable.unpaged();
        return ResponseEntity.ok(ledgerService.search(accountId, transferRef, direction, pageable));
    }
}
