package com.plopvape.notification.config;

import com.plopvape.notification.consumer.OrderEventConsumer;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.data.redis.connection.stream.Consumer;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.connection.stream.ReadOffset;
import org.springframework.data.redis.connection.stream.StreamOffset;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.stream.StreamMessageListenerContainer;
import org.springframework.data.redis.stream.Subscription;

import java.time.Duration;

@Configuration
public class RedisStreamConfig {

    private static final Logger log = LoggerFactory.getLogger(RedisStreamConfig.class);
    private static final String STREAM_KEY = "order-events";
    private static final String GROUP_NAME = "notification-group";
    private static final String CONSUMER_NAME = "notification-consumer-1";

    private final StringRedisTemplate redisTemplate;

    public RedisStreamConfig(StringRedisTemplate redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    @PostConstruct
    public void createConsumerGroup() {
        try {
            redisTemplate.opsForStream().createGroup(STREAM_KEY, GROUP_NAME);
            log.info("Created consumer group '{}' for stream '{}'", GROUP_NAME, STREAM_KEY);
        } catch (Exception e) {
            log.info("Consumer group '{}' already exists or stream not ready: {}", GROUP_NAME, e.getMessage());
        }
    }

    @Bean
    public Subscription orderEventSubscription(RedisConnectionFactory connectionFactory,
                                                OrderEventConsumer consumer) {
        var options = StreamMessageListenerContainer.StreamMessageListenerContainerOptions
                .builder()
                .pollTimeout(Duration.ofSeconds(2))
                .build();

        var container = StreamMessageListenerContainer.create(connectionFactory, options);

        Subscription subscription = container.receive(
                Consumer.from(GROUP_NAME, CONSUMER_NAME),
                StreamOffset.create(STREAM_KEY, ReadOffset.lastConsumed()),
                consumer
        );

        container.start();
        return subscription;
    }
}
