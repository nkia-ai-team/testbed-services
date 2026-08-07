package com.fooddelivery.restaurant.delay;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

/**
 * restaurant 홉의 응답 고정 지연 값. order->restaurant 만 재기동 없이 늦추는 표면 —
 * k8s.env 토글은 롤아웃을 유발하고, HTTP 관리 엔드포인트는 otel javaagent 가 서버
 * 스팬으로 지연 시각을 자백하며, 호출자 파라미터형 제어는 접근 로그가 정답을
 * 자백한다. DB 행 하나면 셋 다 없다(core-banking 의 같은 이름 테이블과 같은 계보).
 *
 * <p>mock 의 /pay 지연은 F19-P(결제 풀 고갈)와 레버가 겹쳐 기각됐다 — 지연은 반드시
 * restaurant 홉에 있어야 두 시나리오의 원인이 갈린다.
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
