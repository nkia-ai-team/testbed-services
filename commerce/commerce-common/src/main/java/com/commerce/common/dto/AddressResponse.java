package com.commerce.common.dto;

public record AddressResponse(
        Long addressId,
        Long userId,
        String recipientName,
        String phone,
        String zipcode,
        String address1,
        String address2,
        boolean isDefault
) {}
