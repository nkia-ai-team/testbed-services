package com.fooddelivery.dispatch.repository;

import com.fooddelivery.dispatch.entity.DispatchEventLog;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;

public interface DispatchEventLogRepository extends JpaRepository<DispatchEventLog, Long> {

    Page<DispatchEventLog> findByDispatchIdOrderByOccurredAtAsc(Long dispatchId, Pageable pageable);
}
