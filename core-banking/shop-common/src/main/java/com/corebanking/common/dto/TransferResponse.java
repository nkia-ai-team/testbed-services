package com.corebanking.common.dto;

/**
 * cross-domain 고정 인터페이스 응답 DTO.
 * status 는 "COMPLETED" 또는 "FAILED".
 */
public record TransferResponse(
        String transferId,
        String status
) {
}
