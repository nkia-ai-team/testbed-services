package com.fooddelivery.dispatch.service;

import com.fooddelivery.common.dto.DispatchEvent;
import com.fooddelivery.common.dto.DispatchEventResponse;
import com.fooddelivery.common.dto.DispatchRequest;
import com.fooddelivery.common.dto.DispatchResponse;
import com.fooddelivery.common.exception.ServiceException;
import com.fooddelivery.dispatch.entity.Dispatch;
import com.fooddelivery.dispatch.entity.DispatchEventLog;
import com.fooddelivery.dispatch.event.DispatchOutboxPublisher;
import com.fooddelivery.dispatch.repository.DispatchEventLogRepository;
import com.fooddelivery.dispatch.repository.DispatchRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.Pageable;
import org.springframework.http.HttpStatus;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;

@Service
public class DispatchService {

    private static final Logger log = LoggerFactory.getLogger(DispatchService.class);

    private final DispatchRepository dispatchRepository;
    private final DispatchEventLogRepository dispatchEventLogRepository;
    private final DispatchOutboxPublisher outboxPublisher;
    private final int maxCapacity;
    private final String dispatchTopic;

    public DispatchService(DispatchRepository dispatchRepository,
                           DispatchEventLogRepository dispatchEventLogRepository,
                           DispatchOutboxPublisher outboxPublisher,
                           @Value("${dispatch.max-capacity:50}") int maxCapacity,
                           @Value("${topics.dispatch}") String dispatchTopic) {
        this.dispatchRepository = dispatchRepository;
        this.dispatchEventLogRepository = dispatchEventLogRepository;
        this.outboxPublisher = outboxPublisher;
        this.maxCapacity = maxCapacity;
        this.dispatchTopic = dispatchTopic;
    }

    @Transactional
    public DispatchResponse dispatchCourier(DispatchRequest request) {
        // order ID 는 request 그대로 신뢰 (microservice 경계).
        // order-service 의 @Transactional 안에서 fan-out 호출 시 commit 전이라 reverse GET 시 404 race.
        // 정합성은 eventual — orphan dispatch 는 외부 reconciliation 작업 책임.
        if (request.orderId() == null) {
            throw new ServiceException(HttpStatus.BAD_REQUEST, "orderId required");
        }

        long currentAssigned = dispatchRepository.countByStatus("ASSIGNED");
        if (currentAssigned >= maxCapacity) {
            throw new ServiceException(HttpStatus.SERVICE_UNAVAILABLE,
                    "Courier pool exhausted (assigned=" + currentAssigned + ", max=" + maxCapacity + ")");
        }

        Dispatch d = new Dispatch();
        d.setOrderId(request.orderId());
        d.setCourierId("courier-" + UUID.randomUUID().toString().substring(0, 8));
        d.setEtaMinutes(15 + ThreadLocalRandom.current().nextInt(20));
        d.setStatus("ASSIGNED");
        dispatchRepository.save(d);
        recordEvent(d, "ASSIGNED");

        log.info("Dispatched courier={} for order={}, eta={}min",
                d.getCourierId(), d.getOrderId(), d.getEtaMinutes());
        return toResponse(d);
    }

    @Transactional(readOnly = true)
    public DispatchResponse getDispatch(Long id) {
        Dispatch d = dispatchRepository.findById(id)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Dispatch not found: " + id));
        return toResponse(d);
    }

    // §7 신규 — status/page/size 필터. 배열 응답(하위호환).
    @Transactional(readOnly = true)
    public List<DispatchResponse> searchDispatches(String status, Pageable pageable) {
        return dispatchRepository.search(status, pageable).getContent().stream()
                .map(this::toResponse)
                .toList();
    }

    // §7 신규 — 배차 상태 전이 이력.
    @Transactional(readOnly = true)
    public List<DispatchEventResponse> getEvents(Long dispatchId, Pageable pageable) {
        if (!dispatchRepository.existsById(dispatchId)) {
            throw new ServiceException(HttpStatus.NOT_FOUND, "Dispatch not found: " + dispatchId);
        }
        return dispatchEventLogRepository.findByDispatchIdOrderByOccurredAtAsc(dispatchId, pageable).getContent().stream()
                .map(e -> new DispatchEventResponse(e.getId(), e.getDispatchId(), e.getStatus(), e.getOccurredAt()))
                .toList();
    }

    @Transactional(readOnly = true)
    public Map<String, Integer> getCapacity() {
        long currentAssigned = dispatchRepository.countByStatus("ASSIGNED");
        int available = Math.max(0, (int) (maxCapacity - currentAssigned));
        Map<String, Integer> result = new LinkedHashMap<>();
        result.put("currentAssigned", (int) currentAssigned);
        result.put("maxCapacity", maxCapacity);
        result.put("available", available);
        return result;
    }

    // 배달 완료 시뮬레이션: ETA 가 지난 ASSIGNED dispatch 는 DELIVERED 로 전이.
    // 없으면 capacity 가 영구히 차서 정상 traffic 도 503. scenario-02 seed (eta=30min) 는
    // 시나리오 duration (240s) 안에 expire X 라 알람 영향 없음.
    @Scheduled(fixedDelay = 30000, initialDelay = 30000)
    @Transactional
    public void deliverExpiredDispatches() {
        List<Dispatch> expired = dispatchRepository.findExpiredAssigned();
        log.info("Dispatch expiry batch started: candidates={}", expired.size());
        for (Dispatch d : expired) {
            d.setStatus("DELIVERED");
            dispatchRepository.save(d);
            recordEvent(d, "DELIVERED");
        }
        log.info("Dispatch expiry batch finished: delivered={}", expired.size());
    }

    private void recordEvent(Dispatch d, String status) {
        DispatchEventLog eventLog = new DispatchEventLog();
        eventLog.setDispatchId(d.getId());
        eventLog.setStatus(status);
        dispatchEventLogRepository.save(eventLog);

        try {
            outboxPublisher.publish(dispatchTopic, "DISPATCH", String.valueOf(d.getId()),
                    "DISPATCH_" + status,
                    new DispatchEvent(d.getId(), d.getOrderId(), status, "DISPATCH_" + status));
        } catch (Exception ex) {
            log.warn("Failed to record dispatch outbox event (non-critical): {}", ex.getMessage());
        }
    }

    private DispatchResponse toResponse(Dispatch d) {
        return new DispatchResponse(
                d.getId(),
                d.getOrderId(),
                d.getCourierId(),
                d.getEtaMinutes(),
                d.getStatus(),
                d.getAssignedAt()
        );
    }
}
