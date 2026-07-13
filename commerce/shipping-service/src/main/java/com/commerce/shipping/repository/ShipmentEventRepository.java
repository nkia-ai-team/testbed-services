package com.commerce.shipping.repository;

import com.commerce.shipping.entity.ShipmentEvent;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface ShipmentEventRepository extends JpaRepository<ShipmentEvent, Long> {

    List<ShipmentEvent> findByShipmentIdOrderByOccurredAtAsc(Long shipmentId);
}
