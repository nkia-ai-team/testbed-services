package com.commerce.shipping.controller;

import com.commerce.common.dto.ShipmentEventResponse;
import com.commerce.common.dto.ShipmentResponse;
import com.commerce.shipping.service.ShippingService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/shipments")
public class ShipmentController {

    private final ShippingService shippingService;

    public ShipmentController(ShippingService shippingService) {
        this.shippingService = shippingService;
    }

    @GetMapping
    public ResponseEntity<List<ShipmentResponse>> getShipments(@RequestParam(required = false) String status) {
        return ResponseEntity.ok(shippingService.getShipments(status));
    }

    @GetMapping("/{id}")
    public ResponseEntity<ShipmentResponse> getShipment(@PathVariable Long id) {
        return ResponseEntity.ok(shippingService.getShipment(id));
    }

    @GetMapping("/order/{orderId}")
    public ResponseEntity<ShipmentResponse> getShipmentByOrderId(@PathVariable Long orderId) {
        return ResponseEntity.ok(shippingService.getShipmentByOrderId(orderId));
    }

    @GetMapping("/{id}/events")
    public ResponseEntity<List<ShipmentEventResponse>> getShipmentEvents(@PathVariable Long id) {
        return ResponseEntity.ok(shippingService.getShipmentEvents(id));
    }
}
