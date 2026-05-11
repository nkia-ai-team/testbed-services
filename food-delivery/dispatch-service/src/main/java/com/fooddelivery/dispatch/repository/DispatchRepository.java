package com.fooddelivery.dispatch.repository;

import com.fooddelivery.dispatch.entity.Dispatch;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;

public interface DispatchRepository extends JpaRepository<Dispatch, Long> {

    long countByStatus(String status);

    // ASSIGNED → DELIVERED 자동 전이: assigned_at + eta_minutes 가 이미 지난 row
    @Modifying
    @Query(value = "UPDATE dispatches SET status='DELIVERED' " +
            "WHERE status='ASSIGNED' AND assigned_at + (eta_minutes * INTERVAL '1 minute') < now()",
            nativeQuery = true)
    int markExpiredAsDelivered();
}
