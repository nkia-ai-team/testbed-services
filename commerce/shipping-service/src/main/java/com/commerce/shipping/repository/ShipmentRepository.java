package com.commerce.shipping.repository;

import com.commerce.shipping.entity.Shipment;
import org.springframework.data.jpa.repository.JpaRepository;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

public interface ShipmentRepository extends JpaRepository<Shipment, Long> {

    Optional<Shipment> findByOrderId(Long orderId);

    List<Shipment> findByStatus(String status);

    List<Shipment> findByStatusAndCreatedAtBefore(String status, LocalDateTime before);

    List<Shipment> findByStatusAndUpdatedAtBefore(String status, LocalDateTime before);
}
