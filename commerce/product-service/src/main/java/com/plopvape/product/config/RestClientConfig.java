package com.plopvape.product.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.time.Duration;

@Configuration
public class RestClientConfig {

    @Value("${services.inventory.url}")
    private String inventoryServiceUrl;

    @Value("${services.inventory.connect-timeout:3s}")
    private Duration connectTimeout;

    @Value("${services.inventory.read-timeout:10s}")
    private Duration readTimeout;

    @Bean
    public RestClient inventoryRestClient() {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(connectTimeout);
        factory.setReadTimeout(readTimeout);

        return RestClient.builder()
                .baseUrl(inventoryServiceUrl)
                .requestFactory(factory)
                .build();
    }
}
