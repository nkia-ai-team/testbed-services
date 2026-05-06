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
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;

@Service
public class DispatchService {

    private static final Logger log = LoggerFactory.getLogger(DispatchService.class);

    private final DispatchRepository dispatchRepository;
    private final OrderClient orderClient;

    public DispatchService(DispatchRepository dispatchRepository, OrderClient orderClient) {
        this.dispatchRepository = dispatchRepository;
        this.orderClient = orderClient;
    }

    @Transactional
    public DispatchResponse dispatchCourier(DispatchRequest request) {
        OrderResponse order = orderClient.getOrder(request.orderId());
        if (order == null) {
            throw new ServiceException(HttpStatus.BAD_REQUEST, "Order not found: " + request.orderId());
        }

        Dispatch d = new Dispatch();
        d.setOrderId(order.id());
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
