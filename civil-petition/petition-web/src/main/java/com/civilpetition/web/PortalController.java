package com.civilpetition.web;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpSession;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.LocalDate;
import java.time.YearMonth;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** 민원 포털 업무 경로(설계 §2-3 표의 일곱 기능). */
@RestController
public class PortalController {

    static final int MAX_RANGE_DAYS = 92;
    private final PetitionRepository petitions;

    public PortalController(PetitionRepository petitions) {
        this.petitions = petitions;
    }

    @GetMapping("/api/health")
    public Map<String, Object> health() {
        return Map.of("status", petitions.ping() == 1 ? "UP" : "DOWN");
    }

    @PostMapping("/api/login")
    public Map<String, Object> login(@RequestBody Map<String, String> form, HttpServletRequest request) {
        String loginId = form.getOrDefault("loginId", "");
        Map<String, Object> user = petitions.findUser(loginId)
                .orElseThrow(() -> new ApiException(HttpStatus.UNAUTHORIZED, "login failed"));
        if (!hash(loginId, form.getOrDefault("password", "")).equals(String.valueOf(user.get("password_hash")).trim())) {
            throw new ApiException(HttpStatus.UNAUTHORIZED, "login failed");
        }
        HttpSession old = request.getSession(false);
        if (old != null) {
            old.invalidate();
        }
        Object dept = user.get("dept_id");
        SessionUser sessionUser = new SessionUser(((Number) user.get("user_id")).longValue(),
                String.valueOf(user.get("role")), dept == null ? null : ((Number) dept).intValue());
        request.getSession(true).setAttribute(SessionUser.KEY, sessionUser);
        return Map.of("userId", sessionUser.userId(), "role", sessionUser.role());
    }

    @PostMapping("/api/petitions")
    @SuppressWarnings("unchecked")
    public Map<String, Object> submit(@RequestBody Map<String, Object> form, HttpSession session) {
        SessionUser user = require(session, false);
        String title = text(form, "title", 200);
        String body = text(form, "body", 4000);
        int deptId = ((Number) form.getOrDefault("deptId", 0)).intValue();
        if (deptId < 1) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "deptId required");
        }
        List<Map<String, Object>> attachments = (List<Map<String, Object>>) form.getOrDefault("attachments", List.of());
        if (attachments.size() > 3) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "too many attachments");
        }
        return petitions.create(user.userId(), deptId, text(form, "category", 20), title, body,
                Boolean.TRUE.equals(form.get("isPublic")), attachments);
    }

    @GetMapping("/api/petitions/{receiptNo}")
    public Map<String, Object> lookup(@PathVariable String receiptNo) {
        if (!receiptNo.matches("CP[0-9]{8}")) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "bad receipt number");
        }
        return petitions.findByReceipt(receiptNo)
                .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "no such petition"));
    }

    @GetMapping("/api/my/petitions")
    public List<Map<String, Object>> mine(@RequestParam(required = false) String status,
                                          @RequestParam LocalDate from, @RequestParam LocalDate to,
                                          @RequestParam(defaultValue = "0") int page, HttpSession session) {
        SessionUser user = require(session, false);
        range(from, to);
        return petitions.listMine(user.userId(), status, from, to, Math.max(0, page));
    }

    @GetMapping("/api/public/petitions")
    public List<Map<String, Object>> search(@RequestParam String keyword, @RequestParam LocalDate from,
                                            @RequestParam LocalDate to, @RequestParam(defaultValue = "0") int page) {
        if (keyword.isBlank() || keyword.length() > 40) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "keyword length");
        }
        range(from, to);
        return petitions.searchPublic(keyword.strip(), from, to, Math.max(0, page));
    }

    @GetMapping("/api/staff/petitions")
    public List<Map<String, Object>> inbox(@RequestParam(defaultValue = "10") int size, HttpSession session) {
        SessionUser staff = require(session, true);
        return petitions.inbox(staff.deptId(), Math.min(Math.max(size, 1), 50));
    }

    @PostMapping("/api/staff/petitions/{petitionId}/process")
    public Map<String, Object> process(@PathVariable long petitionId, @RequestBody Map<String, String> form,
                                       HttpSession session) {
        SessionUser staff = require(session, true);
        String note = form.getOrDefault("note", "");
        if (note.length() > 400) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "note too long");
        }
        return petitions.process(petitionId, staff.userId(), staff.deptId(), form.getOrDefault("action", ""), note);
    }

    @GetMapping("/api/stats/departments")
    public List<Map<String, Object>> stats(@RequestParam YearMonth month) {
        return petitions.departmentStats(month.atDay(1));
    }

    static String hash(String loginId, String password) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(digest.digest((loginId + ":" + password).getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException(error);
        }
    }

    private static SessionUser require(HttpSession session, boolean staff) {
        SessionUser user = SessionUser.of(session);
        if (user == null) {
            throw new ApiException(HttpStatus.UNAUTHORIZED, "login required");
        }
        if (staff && !user.isStaff()) {
            throw new ApiException(HttpStatus.FORBIDDEN, "staff only");
        }
        return user;
    }

    private static String text(Map<String, Object> form, String key, int max) {
        Object value = form.get(key);
        if (!(value instanceof String text) || text.isBlank() || text.length() > max) {
            throw new ApiException(HttpStatus.BAD_REQUEST, key + " invalid");
        }
        return text;
    }

    private static void range(LocalDate from, LocalDate to) {
        if (to.isBefore(from) || from.plusDays(MAX_RANGE_DAYS).isBefore(to)) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "date range");
        }
    }
}
