package com.fooddelivery.dispatch.repository;

import com.fooddelivery.dispatch.entity.Dispatch;
import org.springframework.data.jpa.repository.JpaRepository;

public interface DispatchRepository extends JpaRepository<Dispatch, Long> {

    long countByStatus(String status);
}
