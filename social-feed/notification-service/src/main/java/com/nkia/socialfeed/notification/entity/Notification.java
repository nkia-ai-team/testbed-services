package com.nkia.socialfeed.notification.entity;

import com.nkia.socialfeed.common.config.BaseEntity;
import jakarta.persistence.*;

@Entity
@Table(name = "notifications", indexes = {
        @Index(name = "idx_notif_user", columnList = "user_id")
})
public class Notification extends BaseEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "user_id", nullable = false)
    private Long userId;

    @Column(name = "type", length = 32)
    private String type;

    @Column(name = "ref_id")
    private Long refId;

    @Column(name = "read_flag")
    private Integer readFlag = 0;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getUserId() { return userId; }
    public void setUserId(Long userId) { this.userId = userId; }
    public String getType() { return type; }
    public void setType(String type) { this.type = type; }
    public Long getRefId() { return refId; }
    public void setRefId(Long refId) { this.refId = refId; }
    public Integer getReadFlag() { return readFlag; }
    public void setReadFlag(Integer readFlag) { this.readFlag = readFlag; }

    public boolean isRead() { return readFlag != null && readFlag == 1; }
}
