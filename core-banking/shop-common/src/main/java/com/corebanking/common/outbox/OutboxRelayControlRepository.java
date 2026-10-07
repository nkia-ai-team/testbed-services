package com.corebanking.common.outbox;

import org.springframework.data.jpa.repository.JpaRepository;

public interface OutboxRelayControlRepository extends JpaRepository<OutboxRelayControl, String> {
}
