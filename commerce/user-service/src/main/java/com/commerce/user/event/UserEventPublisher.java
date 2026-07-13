package com.commerce.user.event;

import com.commerce.common.dto.UserEvent;
import com.commerce.common.outbox.OutboxPublisher;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public class UserEventPublisher {

    private static final Logger log = LoggerFactory.getLogger(UserEventPublisher.class);

    private final OutboxPublisher outboxPublisher;
    private final String usersTopic;

    public UserEventPublisher(OutboxPublisher outboxPublisher,
                               @Value("${topics.users}") String usersTopic) {
        this.outboxPublisher = outboxPublisher;
        this.usersTopic = usersTopic;
    }

    public void publish(UserEvent event) {
        outboxPublisher.publish(usersTopic, "USER", String.valueOf(event.userId()),
                event.eventType(), event);
        log.info("Recorded outbox event for user: userId={}, eventType={}", event.userId(), event.eventType());
    }
}
