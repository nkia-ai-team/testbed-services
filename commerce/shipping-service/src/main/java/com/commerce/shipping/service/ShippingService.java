package com.commerce.shipping.service;

import com.commerce.common.dto.ShipmentEventResponse;
import com.commerce.common.dto.ShipmentResponse;
import com.commerce.common.dto.ShippingEvent;
import com.commerce.common.exception.ServiceException;
import com.commerce.shipping.entity.Shipment;
import com.commerce.shipping.entity.ShipmentEvent;
import com.commerce.shipping.event.ShippingEventPublisher;
import com.commerce.shipping.repository.ShipmentEventRepository;
import com.commerce.shipping.repository.ShipmentRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.UUID;

@Service
public class ShippingService {

    private static final Logger log = LoggerFactory.getLogger(ShippingService.class);

    private final ShipmentRepository shipmentRepository;
    private final ShipmentEventRepository shipmentEventRepository;
    private final ShippingEventPublisher eventPublisher;
    private final long shipAfterMinutes;
    private final long deliverAfterMinutes;

    public ShippingService(ShipmentRepository shipmentRepository,
                            ShipmentEventRepository shipmentEventRepository,
                            ShippingEventPublisher eventPublisher,
                            @Value("${shipment.transition.ship-after-minutes:2}") long shipAfterMinutes,
                            @Value("${shipment.transition.deliver-after-minutes:5}") long deliverAfterMinutes) {
        this.shipmentRepository = shipmentRepository;
        this.shipmentEventRepository = shipmentEventRepository;
        this.eventPublisher = eventPublisher;
        this.shipAfterMinutes = shipAfterMinutes;
        this.deliverAfterMinutes = deliverAfterMinutes;
    }

    // Kafka 구독 경로에서 호출 — 같은 orderId로 여러 번 들어와도(재전송, 컨슈머 재시작) 멱등하게 무시한다.
    @Transactional
    public void createShipmentForOrder(Long orderId) {
        if (shipmentRepository.findByOrderId(orderId).isPresent()) {
            log.debug("Shipment already exists for orderId={}, skipping", orderId);
            return;
        }

        Shipment shipment = new Shipment();
        shipment.setOrderId(orderId);
        shipment.setStatus("CREATED");
        shipment.setTrackingNumber(UUID.randomUUID().toString().substring(0, 12).toUpperCase());
        shipmentRepository.save(shipment);

        recordEvent(shipment, "CREATED");
        log.info("Created shipment for orderId={}, shipmentId={}", orderId, shipment.getId());
    }

    // 시나리오 밖 평상시 배경 부하 — CREATED→SHIPPED→DELIVERED를 시간 경과에 따라 자동 전이시킨다.
    @Scheduled(fixedDelayString = "${shipment.transition.poll-interval-ms:30000}")
    @Transactional
    public void transitionShipments() {
        LocalDateTime shipCutoff = LocalDateTime.now().minusMinutes(shipAfterMinutes);
        for (Shipment shipment : shipmentRepository.findByStatusAndCreatedAtBefore("CREATED", shipCutoff)) {
            shipment.setStatus("SHIPPED");
            shipmentRepository.save(shipment);
            recordEvent(shipment, "SHIPPED");
        }

        LocalDateTime deliverCutoff = LocalDateTime.now().minusMinutes(deliverAfterMinutes);
        for (Shipment shipment : shipmentRepository.findByStatusAndUpdatedAtBefore("SHIPPED", deliverCutoff)) {
            shipment.setStatus("DELIVERED");
            shipmentRepository.save(shipment);
            recordEvent(shipment, "DELIVERED");
        }
    }

    @Transactional(readOnly = true)
    public ShipmentResponse getShipment(Long id) {
        return toResponse(findById(id));
    }

    @Transactional(readOnly = true)
    public ShipmentResponse getShipmentByOrderId(Long orderId) {
        return shipmentRepository.findByOrderId(orderId)
                .map(this::toResponse)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Shipment not found for order: " + orderId));
    }

    @Transactional(readOnly = true)
    public List<ShipmentResponse> getShipments(String status) {
        List<Shipment> shipments = status != null ? shipmentRepository.findByStatus(status) : shipmentRepository.findAll();
        return shipments.stream().map(this::toResponse).toList();
    }

    @Transactional(readOnly = true)
    public List<ShipmentEventResponse> getShipmentEvents(Long shipmentId) {
        findById(shipmentId); // 존재 확인
        return shipmentEventRepository.findByShipmentIdOrderByOccurredAtAsc(shipmentId).stream()
                .map(e -> new ShipmentEventResponse(e.getId(), e.getShipmentId(), e.getStatus(), e.getOccurredAt()))
                .toList();
    }

    private void recordEvent(Shipment shipment, String status) {
        ShipmentEvent event = new ShipmentEvent();
        event.setShipmentId(shipment.getId());
        event.setStatus(status);
        shipmentEventRepository.save(event);

        try {
            eventPublisher.publish(new ShippingEvent(shipment.getId(), shipment.getOrderId(), status, "SHIPMENT_" + status));
        } catch (Exception ex) {
            log.warn("Failed to publish shipping event (non-critical): {}", ex.getMessage());
        }
    }

    private Shipment findById(Long id) {
        return shipmentRepository.findById(id)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Shipment not found: " + id));
    }

    private ShipmentResponse toResponse(Shipment shipment) {
        return new ShipmentResponse(
                shipment.getId(),
                shipment.getOrderId(),
                shipment.getStatus(),
                shipment.getTrackingNumber(),
                shipment.getCarrier(),
                shipment.getCreatedAt(),
                shipment.getUpdatedAt()
        );
    }
}
