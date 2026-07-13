package com.fooddelivery.dispatch.entity;

import jakarta.persistence.*;
import java.time.LocalDateTime;

@Entity
@Table(name = "dispatch_events")
public class DispatchEventLog {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "dispatch_id", nullable = false)
    private Long dispatchId;

    @Column(name = "status", nullable = false, length = 16)
    private String status;

    @Column(name = "occurred_at")
    private LocalDateTime occurredAt = LocalDateTime.now();

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getDispatchId() { return dispatchId; }
    public void setDispatchId(Long dispatchId) { this.dispatchId = dispatchId; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public LocalDateTime getOccurredAt() { return occurredAt; }
}
