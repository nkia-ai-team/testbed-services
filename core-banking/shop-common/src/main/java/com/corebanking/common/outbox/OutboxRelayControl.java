package com.corebanking.common.outbox;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

/**
 * 릴레이 일시정지 플래그. 재기동 없이 릴레이를 멈추는 유일한 표면 —
 * OUTBOX_RELAY_ENABLED(env)는 값을 바꾸면 롤아웃이 돌고, testbed-transfer 는
 * maxSurge=0 이라 유일한 파드가 먼저 내려간다. HTTP 관리 엔드포인트는 otel
 * javaagent 가 서버 스팬을 남겨 정지 시각이 트레이스에 그대로 찍힌다.
 * DB 행 하나면 둘 다 피한다 — 릴레이는 어차피 2초마다 DB를 폴링한다.
 */
@Entity
@Table(name = "outbox_relay_control")
public class OutboxRelayControl {

    @Id
    @Column(name = "service_id", length = 32)
    private String serviceId;

    @Column(name = "enabled", nullable = false)
    private boolean enabled = true;

    @Column(name = "updated_at")
    private LocalDateTime updatedAt;

    public String getServiceId() {
        return serviceId;
    }

    public boolean isEnabled() {
        return enabled;
    }

    public LocalDateTime getUpdatedAt() {
        return updatedAt;
    }
}
