package com.commerce.shipping.entity;

import com.commerce.common.config.BaseEntity;
import jakarta.persistence.*;

@Entity
@Table(name = "shipments", schema = "shipping_schema")
public class Shipment extends BaseEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "order_id", nullable = false, unique = true)
    private Long orderId;

    @Column(nullable = false, length = 20)
    private String status = "CREATED";

    @Column(name = "tracking_number", length = 50)
    private String trackingNumber;

    @Column(nullable = false, length = 50)
    private String carrier = "CJ대한통운";

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getOrderId() { return orderId; }
    public void setOrderId(Long orderId) { this.orderId = orderId; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public String getTrackingNumber() { return trackingNumber; }
    public void setTrackingNumber(String trackingNumber) { this.trackingNumber = trackingNumber; }
    public String getCarrier() { return carrier; }
    public void setCarrier(String carrier) { this.carrier = carrier; }
}
