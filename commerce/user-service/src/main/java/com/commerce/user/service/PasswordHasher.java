package com.commerce.user.service;

import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

/**
 * 테스트베드용 최소 해시. 서명된 JWT나 BCrypt까지 갈 필요 없는 범위라
 * 고정 salt + SHA-256으로 단순화했다(실서비스 보안 요구 수준 아님).
 */
@Component
public class PasswordHasher {

    private static final String SALT = "user-salt-v1";

    public String hash(String rawPassword) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] hashed = digest.digest((rawPassword + "|" + SALT).getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(hashed);
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException("SHA-256 not available", ex);
        }
    }

    public boolean matches(String rawPassword, String hash) {
        return hash(rawPassword).equals(hash);
    }
}
