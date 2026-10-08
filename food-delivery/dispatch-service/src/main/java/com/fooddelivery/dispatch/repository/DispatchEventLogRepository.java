package com.fooddelivery.dispatch.repository;

import com.fooddelivery.dispatch.entity.DispatchEventLog;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;

public interface DispatchEventLogRepository extends JpaRepository<DispatchEventLog, Long> {

    Page<DispatchEventLog> findByDispatchIdOrderByOccurredAtAsc(Long dispatchId, Pageable pageable);

    // 보존 기간 정리용. 배차를 지우기 전에 그 배차의 전이 이력을 idx_dispatch_events_dispatch 로 지운다.
    @Modifying
    @Query("delete from DispatchEventLog e where e.dispatchId in :dispatchIds")
    int deleteByDispatchIdIn(@Param("dispatchIds") Collection<Long> dispatchIds);
}
