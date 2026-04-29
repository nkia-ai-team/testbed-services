package com.plopvape.order.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.time.Duration;

@Configuration
public class RestClientConfig {

    @Bean
    public RestClient productRestClient(
            @Value("${services.product.url}") String baseUrl,
            @Value("${services.product.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.product.read-timeout:10s}") Duration readTimeout) {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(connectTimeout);
        factory.setReadTimeout(readTimeout);
        return RestClient.builder().baseUrl(baseUrl).requestFactory(factory).build();
    }

    @Bean
    public RestClient paymentRestClient(
            @Value("${services.payment.url}") String baseUrl,
            @Value("${services.payment.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.payment.read-timeout:15s}") Duration readTimeout) {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(connectTimeout);
        factory.setReadTimeout(readTimeout);
        return RestClient.builder().baseUrl(baseUrl).requestFactory(factory).build();
    }

    @Bean
    public RestClient inventoryRestClient(
            @Value("${services.inventory.url}") String baseUrl,
            @Value("${services.inventory.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.inventory.read-timeout:10s}") Duration readTimeout) {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(connectTimeout);
        factory.setReadTimeout(readTimeout);
        return RestClient.builder().baseUrl(baseUrl).requestFactory(factory).build();
    }
}
