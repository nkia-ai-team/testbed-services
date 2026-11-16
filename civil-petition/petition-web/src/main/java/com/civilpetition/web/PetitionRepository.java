package com.civilpetition.web;

import java.sql.Timestamp;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

/** 민원 표 접근. SQL 은 설계 §2-3 표의 DB 접근 꼴(기본 키, 인덱스 범위, LIKE 와 기간, 짧은 행 잠금)을 따른다. */
@Repository
public class PetitionRepository {

    static final int PAGE_SIZE = 20;
    private final JdbcTemplate jdbc;

    public PetitionRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public Optional<Map<String, Object>> findUser(String loginId) {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT user_id, password_hash, role, dept_id FROM app_user WHERE login_id = ?", loginId);
        return rows.stream().findFirst();
    }

    @Transactional
    public Map<String, Object> create(long userId, int deptId, String category, String title, String body,
                                      boolean isPublic, List<Map<String, Object>> attachments) {
        Map<String, Object> row = jdbc.queryForMap(
                "INSERT INTO petition (receipt_no, user_id, dept_id, category, title, body, status, is_public, created_at, updated_at) "
                        + "VALUES ('CP' || lpad(nextval('petition_receipt_seq')::text, 8, '0'), ?, ?, ?, ?, ?, 'RECEIVED', ?, now(), now()) "
                        + "RETURNING petition_id, receipt_no, status, created_at",
                userId, deptId, category, title, body, isPublic);
        long petitionId = ((Number) row.get("petition_id")).longValue();
        jdbc.update("INSERT INTO petition_history (petition_id, from_status, to_status, actor_id, note, created_at) "
                + "VALUES (?, NULL, 'RECEIVED', ?, 'submitted', now())", petitionId, userId);
        for (Map<String, Object> attachment : attachments) {
            jdbc.update("INSERT INTO petition_attachment (petition_id, file_name, size_bytes, created_at) VALUES (?, ?, ?, now())",
                    petitionId, String.valueOf(attachment.get("fileName")),
                    ((Number) attachment.getOrDefault("sizeBytes", 0)).intValue());
        }
        return row;
    }

    public Optional<Map<String, Object>> findByReceipt(String receiptNo) {
        return jdbc.queryForList(
                "SELECT p.petition_id, p.receipt_no, p.category, p.title, p.status, p.created_at, p.updated_at, d.name AS department "
                        + "FROM petition p JOIN department d ON d.dept_id = p.dept_id WHERE p.receipt_no = ?",
                receiptNo).stream().findFirst();
    }

    public List<Map<String, Object>> listMine(long userId, String status, LocalDate from, LocalDate to, int page) {
        return jdbc.queryForList(
                "SELECT petition_id, receipt_no, title, status, created_at FROM petition "
                        + "WHERE user_id = ? AND created_at >= ? AND created_at < ? AND (?::text IS NULL OR status = ?) "
                        + "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                userId, start(from), start(to.plusDays(1)), status, status, PAGE_SIZE, page * PAGE_SIZE);
    }

    public List<Map<String, Object>> searchPublic(String keyword, LocalDate from, LocalDate to, int page) {
        return jdbc.queryForList(
                "SELECT petition_id, receipt_no, title, status, created_at FROM petition "
                        + "WHERE is_public AND created_at >= ? AND created_at < ? AND title LIKE ? "
                        + "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                start(from), start(to.plusDays(1)), "%" + keyword + "%", PAGE_SIZE, page * PAGE_SIZE);
    }

    public List<Map<String, Object>> inbox(int deptId, int size) {
        return jdbc.queryForList(
                "SELECT petition_id, receipt_no, title, status, created_at FROM petition "
                        + "WHERE dept_id = ? AND status IN ('RECEIVED', 'IN_PROGRESS') ORDER BY created_at LIMIT ?",
                deptId, size);
    }

    /** 담당자 처리: 행을 짧게 잠그고 상태를 한 단계 옮긴다. ANSWER 는 바로 완료(답변 등록). */
    @Transactional
    public Map<String, Object> process(long petitionId, long staffId, int deptId, String action, String note) {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT status, dept_id FROM petition WHERE petition_id = ? FOR UPDATE", petitionId);
        if (rows.isEmpty()) {
            throw new ApiException(org.springframework.http.HttpStatus.NOT_FOUND, "no such petition");
        }
        String status = (String) rows.get(0).get("status");
        int owner = ((Number) rows.get(0).get("dept_id")).intValue();
        if (owner != deptId) {
            throw new ApiException(org.springframework.http.HttpStatus.FORBIDDEN, "other department");
        }
        String next = Workflow.next(status, action);
        jdbc.update("UPDATE petition SET status = ?, updated_at = now() WHERE petition_id = ?", next, petitionId);
        jdbc.update("INSERT INTO petition_history (petition_id, from_status, to_status, actor_id, note, created_at) "
                + "VALUES (?, ?, ?, ?, ?, now())", petitionId, status, next, staffId, note);
        if ("COMPLETED".equals(next)) {
            jdbc.update("INSERT INTO petition_answer (petition_id, staff_id, body, answered_at) VALUES (?, ?, ?, now()) "
                    + "ON CONFLICT (petition_id) DO UPDATE SET staff_id = EXCLUDED.staff_id, body = EXCLUDED.body, "
                    + "answered_at = EXCLUDED.answered_at", petitionId, staffId, note);
        }
        return Map.of("petitionId", petitionId, "from", status, "to", next);
    }

    public List<Map<String, Object>> departmentStats(LocalDate month) {
        return jdbc.queryForList(
                "SELECT d.dept_id, d.name, s.received, s.completed, s.avg_days FROM stats_dept_monthly s "
                        + "JOIN department d ON d.dept_id = s.dept_id WHERE s.month = ? ORDER BY s.received DESC LIMIT 50",
                month.withDayOfMonth(1));
    }

    public int ping() {
        Integer one = jdbc.queryForObject("SELECT 1", Integer.class);
        return one == null ? 0 : one;
    }

    private static Timestamp start(LocalDate day) {
        return Timestamp.from(day.atStartOfDay(java.time.ZoneOffset.UTC).toInstant());
    }
}
