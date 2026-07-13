package com.commerce.pricing;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

// pricing-service는 이벤트를 발행하지 않으므로(outbox 미사용) commerce-common outbox 스캔이 필요 없다.
@SpringBootApplication
public class PricingServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(PricingServiceApplication.class, args);
    }
}
