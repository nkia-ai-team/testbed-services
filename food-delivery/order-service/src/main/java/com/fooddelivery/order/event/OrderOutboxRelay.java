package com.fooddelivery.order.event;

import com.fooddelivery.common.outbox.OutboxRelay;
import com.fooddelivery.order.entity.OrderOutboxEvent;
import com.fooddelivery.order.repository.OrderOutboxEventRepository;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(prefix = "outbox.relay", name = "enabled", havingValue = "true")
public class OrderOutboxRelay extends OutboxRelay<OrderOutboxEvent> {

    public OrderOutboxRelay(OrderOutboxEventRepository repository, KafkaTemplate<String, String> kafkaTemplate) {
        super(repository, kafkaTemplate);
    }

    @Scheduled(fixedDelayString = "${outbox.relay.poll-interval-ms:2000}")
    public void poll() {
        relay();
    }
}
