package com.fooddelivery.dispatch.service;

import com.fooddelivery.common.dto.DispatchRequest;
import com.fooddelivery.common.dto.DispatchResponse;
import com.fooddelivery.common.dto.OrderResponse;
import com.fooddelivery.common.exception.ServiceException;
import com.fooddelivery.dispatch.client.OrderClient;
import com.fooddelivery.dispatch.entity.Dispatch;
import com.fooddelivery.dispatch.repository.DispatchRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;

@Service
public class DispatchService {

    private static final Logger log = LoggerFactory.getLogger(DispatchService.class);

    private final DispatchRepository dispatchRepository;
    private final OrderClient orderClient;
    private final int maxCapacity;

    public DispatchService(DispatchRepository dispatchRepository,
                           OrderClient orderClient,
                           @Value("${dispatch.max-capacity:50}") int maxCapacity) {
        this.dispatchRepository = dispatchRepository;
        this.orderClient = orderClient;
        this.maxCapacity = maxCapacity;
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
