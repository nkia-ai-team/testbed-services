package com.commerce.common.dto;

public record UserRegisterRequest(
        String email,
        String password,
        String name,
        String phone
) {}
