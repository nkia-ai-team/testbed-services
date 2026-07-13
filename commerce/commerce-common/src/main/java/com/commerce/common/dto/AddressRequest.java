package com.commerce.common.dto;

public record AddressRequest(
        String recipientName,
        String phone,
        String zipcode,
        String address1,
        String address2,
        Boolean isDefault
) {}
