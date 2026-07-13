package com.commerce.payment.entity;

import jakarta.persistence.*;
import java.math.BigDecimal;
import java.time.LocalDateTime;

@Entity
@Table(name = "settlement_summary", schema = "payment_schema")
public class SettlementSummary {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "period_start", nullable = false)
    private LocalDateTime periodStart;

    @Column(name = "period_end", nullable = false)
    private LocalDateTime periodEnd;

    @Column(name = "payment_count", nullable = false)
    private int paymentCount;

    @Column(name = "total_amount", nullable = false, precision = 14, scale = 2)
    private BigDecimal totalAmount;

    @Column(name = "banking_transfer_status", nullable = false, length = 20)
    private String bankingTransferStatus = "PENDING";

    @Column(name = "banking_transfer_ref", length = 100)
    private String bankingTransferRef;

    @Column(name = "created_at", updatable = false)
    private LocalDateTime createdAt = LocalDateTime.now();

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public LocalDateTime getPeriodStart() { return periodStart; }
    public void setPeriodStart(LocalDateTime periodStart) { this.periodStart = periodStart; }
    public LocalDateTime getPeriodEnd() { return periodEnd; }
    public void setPeriodEnd(LocalDateTime periodEnd) { this.periodEnd = periodEnd; }
    public int getPaymentCount() { return paymentCount; }
    public void setPaymentCount(int paymentCount) { this.paymentCount = paymentCount; }
    public BigDecimal getTotalAmount() { return totalAmount; }
    public void setTotalAmount(BigDecimal totalAmount) { this.totalAmount = totalAmount; }
    public String getBankingTransferStatus() { return bankingTransferStatus; }
    public void setBankingTransferStatus(String bankingTransferStatus) { this.bankingTransferStatus = bankingTransferStatus; }
    public String getBankingTransferRef() { return bankingTransferRef; }
    public void setBankingTransferRef(String bankingTransferRef) { this.bankingTransferRef = bankingTransferRef; }
    public LocalDateTime getCreatedAt() { return createdAt; }
}
