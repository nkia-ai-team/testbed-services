package com.fooddelivery.dispatch.event;

import com.fooddelivery.common.outbox.OutboxRelay;
import com.fooddelivery.dispatch.entity.DispatchOutboxEvent;
import com.fooddelivery.dispatch.repository.DispatchOutboxEventRepository;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(prefix = "outbox.relay", name = "enabled", havingValue = "true")
public class DispatchOutboxRelay extends OutboxRelay<DispatchOutboxEvent> {

    public DispatchOutboxRelay(DispatchOutboxEventRepository repository, KafkaTemplate<String, String> kafkaTemplate) {
        super(repository, kafkaTemplate);
    }

    @Scheduled(fixedDelayString = "${outbox.relay.poll-interval-ms:2000}")
    public void poll() {
        relay();
    }
}
