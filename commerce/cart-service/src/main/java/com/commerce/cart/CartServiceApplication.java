package com.commerce.cart;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

// cart-service는 outbox를 쓰지 않으므로(이벤트 발행 없음) commerce-common outbox 스캔이 필요 없다.
@SpringBootApplication
public class CartServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(CartServiceApplication.class, args);
    }
}
