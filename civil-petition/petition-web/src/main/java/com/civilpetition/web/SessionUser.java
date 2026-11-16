package com.civilpetition.web;

import jakarta.servlet.http.HttpSession;

/** 세션에 담는 로그인 사용자. */
public record SessionUser(long userId, String role, Integer deptId) {

    static final String KEY = "petition.user";

    static SessionUser of(HttpSession session) {
        if (session == null) {
            return null;
        }
        Object value = session.getAttribute(KEY);
        return value instanceof SessionUser user ? user : null;
    }

    boolean isStaff() {
        return "STAFF".equals(role);
    }
}
