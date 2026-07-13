package com.commerce.gateway.config;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.time.Duration;

@Configuration
public class RestClientConfig {

    @Bean
    @Qualifier("userRestClient")
    public RestClient userRestClient(
            @Value("${services.user.url}") String baseUrl,
            @Value("${services.user.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.user.read-timeout:10s}") Duration readTimeout) {
        return build(baseUrl, connectTimeout, readTimeout);
    }

    @Bean
    @Qualifier("cartRestClient")
    public RestClient cartRestClient(
            @Value("${services.cart.url}") String baseUrl,
            @Value("${services.cart.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.cart.read-timeout:10s}") Duration readTimeout) {
        return build(baseUrl, connectTimeout, readTimeout);
    }

    @Bean
    @Qualifier("pricingRestClient")
    public RestClient pricingRestClient(
            @Value("${services.pricing.url}") String baseUrl,
            @Value("${services.pricing.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.pricing.read-timeout:10s}") Duration readTimeout) {
        return build(baseUrl, connectTimeout, readTimeout);
    }

    @Bean
    @Qualifier("shippingRestClient")
    public RestClient shippingRestClient(
            @Value("${services.shipping.url}") String baseUrl,
            @Value("${services.shipping.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.shipping.read-timeout:10s}") Duration readTimeout) {
        return build(baseUrl, connectTimeout, readTimeout);
    }

    @Bean
    @Qualifier("orderRestClient")
    public RestClient orderRestClient(
            @Value("${services.order.url}") String baseUrl,
            @Value("${services.order.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.order.read-timeout:15s}") Duration readTimeout) {
        return build(baseUrl, connectTimeout, readTimeout);
    }

    @Bean
    @Qualifier("productRestClient")
    public RestClient productRestClient(
            @Value("${services.product.url}") String baseUrl,
            @Value("${services.product.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.product.read-timeout:10s}") Duration readTimeout) {
        return build(baseUrl, connectTimeout, readTimeout);
    }

    private RestClient build(String baseUrl, Duration connectTimeout, Duration readTimeout) {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(connectTimeout);
        factory.setReadTimeout(readTimeout);
        return RestClient.builder().baseUrl(baseUrl).requestFactory(factory).build();
    }
}
