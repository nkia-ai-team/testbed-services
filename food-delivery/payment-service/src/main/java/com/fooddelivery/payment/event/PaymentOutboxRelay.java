package com.fooddelivery.payment.event;

import com.fooddelivery.common.outbox.OutboxRelay;
import com.fooddelivery.payment.entity.PaymentOutboxEvent;
import com.fooddelivery.payment.repository.PaymentOutboxEventRepository;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(prefix = "outbox.relay", name = "enabled", havingValue = "true")
public class PaymentOutboxRelay extends OutboxRelay<PaymentOutboxEvent> {

    public PaymentOutboxRelay(PaymentOutboxEventRepository repository, KafkaTemplate<String, String> kafkaTemplate) {
        super(repository, kafkaTemplate);
    }

    @Scheduled(fixedDelayString = "${outbox.relay.poll-interval-ms:2000}")
    public void poll() {
        relay();
    }
}
