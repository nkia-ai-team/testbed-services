package com.fooddelivery.dispatch.repository;

import com.fooddelivery.dispatch.entity.Dispatch;
import org.springframework.data.domain.Slice;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface DispatchRepository extends JpaRepository<Dispatch, Long> {

    long countByStatus(String status);

    // 목록 조회(GET /api/deliveries): status 가 있으면 idx_dispatches_status_assigned 를 타는 파생 쿼리, 없으면 전체. Slice 라 COUNT 를 내지 않는다.
    Slice<Dispatch> findByStatus(String status, Pageable pageable);
    Slice<Dispatch> findAllBy(Pageable pageable);

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

    /** 보존 기간 정리 대상 판정에 필요한 세 컬럼만 읽는다. */
    interface RetentionHead {
        Long getId();

        String getStatus();

        java.time.LocalDateTime getAssignedAt();
    }

    // 보존 기간 정리용. PK 만 타고 커서 뒤 한 묶음을 읽는다 — status·assigned_at 인덱스에 기대지
    // 않는 것은 F33-R 이 그 인덱스를 지우기 때문이다(그때도 정리가 전수 스캔이 되지 않는다).
    @Query("select d.id as id, d.status as status, d.assignedAt as assignedAt from Dispatch d "
            + "where d.id > :afterId order by d.id asc")
    List<RetentionHead> findRetentionHeads(@Param("afterId") long afterId, Pageable pageable);
}
