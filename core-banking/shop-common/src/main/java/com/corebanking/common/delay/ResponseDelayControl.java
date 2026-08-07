package com.corebanking.common.delay;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

/**
 * 서비스 응답 고정 지연 값. 특정 홉만 재기동 없이 느리게 만드는 유일한 표면 —
 * k8s.env 토글은 값을 바꾸면 롤아웃이 돌고(maxSurge=0 아래서 유일한 파드가 먼저
 * 내려간다), HTTP 관리 엔드포인트는 otel javaagent 가 서버 스팬을 남겨 지연 시각과
 * 수단을 트레이스에 자백하며, 호출자 파라미터형 제어(?delayMs=)는 접근 로그가
 * 정답을 자백한다. DB 행 하나면 셋 다 없다 — outbox_relay_control(F18-P)과 같은
 * 계보이고, 앱은 어차피 DB 를 상시 조회한다.
 */
@Entity
@Table(name = "response_delay_control")
public class ResponseDelayControl {

    @Id
    @Column(name = "service_id", length = 32)
    private String serviceId;

    @Column(name = "delay_ms", nullable = false)
    private long delayMs = 0L;

    @Column(name = "updated_at")
    private LocalDateTime updatedAt;

    public String getServiceId() {
        return serviceId;
    }

    public long getDelayMs() {
        return delayMs;
    }

    public LocalDateTime getUpdatedAt() {
        return updatedAt;
    }
}
