package com.fooddelivery.dispatch.repository;

import com.fooddelivery.dispatch.entity.Dispatch;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface DispatchRepository extends JpaRepository<Dispatch, Long> {

    long countByStatus(String status);

    // §7 신규 — status 선택적 필터.
    @Query("SELECT d FROM Dispatch d WHERE (:status IS NULL OR d.status = :status)")
    Page<Dispatch> search(@Param("status") String status, Pageable pageable);

    // ASSIGNED → DELIVERED 자동 전이 대상 조회.
    // (7번 이식 중 발견한 버그 수정) 기존엔 PostgreSQL 문법(assigned_at + (eta_minutes * INTERVAL
    // '1 minute')) 이 MySQL 백엔드 서비스에 그대로 박혀 있었다 — MySQL은 이 문법을 지원하지
    // 않아 이 쿼리는 실행 시 에러가 났을 것이다(commerce에서 복사해오며 생긴 오류로 추정).
    // MySQL 표준 문법(DATE_ADD)으로 정정하고, 벌크 UPDATE 대신 SELECT로 바꿔 배치가 전이된
    // 행별로 dispatch_events 기록 + outbox 이벤트를 남길 수 있게 했다.
    @Query(value = "SELECT * FROM dispatches WHERE status = 'ASSIGNED' "
            + "AND DATE_ADD(assigned_at, INTERVAL eta_minutes MINUTE) < NOW()",
            nativeQuery = true)
    List<Dispatch> findExpiredAssigned();
}
