package com.civilpetition.web;

import java.time.Instant;
import java.time.LocalTime;
import java.time.ZoneId;

/**
 * 보관 배치 설정(BatchService archive). 값의 정본은 application.properties 의 petition.archive.* 이고
 * 환경변수(PETITION_ARCHIVE_KEEP_ROWS 등)로 덮는다.
 *
 * @param keepRows    민원 표에 남길 건수 상한. 넘은 만큼 가장 오래된 것부터 옮긴다
 * @param chunkRows   한 번에 옮기는 민원 수(한 문장, 한 트랜잭션)
 * @param pauseMillis 묶음 사이 쉼
 * @param windowStart 배치 시간 창 시작(포함)
 * @param windowEnd   배치 시간 창 끝(제외). 시작보다 이르면 자정을 넘는 창이다
 * @param zone        창을 읽는 시간대
 */
record ArchivePolicy(long keepRows, int chunkRows, long pauseMillis, LocalTime windowStart, LocalTime windowEnd, ZoneId zone) {

    static final int MAX_CHUNK_ROWS = 50_000;

    ArchivePolicy {
        if (keepRows < 1) {
            throw new IllegalArgumentException("petition.archive.keep-rows must be positive");
        }
        if (chunkRows < 1 || chunkRows > MAX_CHUNK_ROWS) {
            throw new IllegalArgumentException("petition.archive.chunk-rows must be 1.." + MAX_CHUNK_ROWS);
        }
        if (pauseMillis < 0) {
            throw new IllegalArgumentException("petition.archive.pause-ms must not be negative");
        }
        if (windowStart.equals(windowEnd)) {
            throw new IllegalArgumentException("petition.archive.window must not be empty");
        }
    }

    /** window 는 "HH:MM-HH:MM" 꼴이다. */
    static ArchivePolicy of(long keepRows, int chunkRows, long pauseMillis, String window, String zone) {
        String[] ends = window.trim().split("-");
        if (ends.length != 2) {
            throw new IllegalArgumentException("petition.archive.window must look like 02:00-05:00");
        }
        return new ArchivePolicy(keepRows, chunkRows, pauseMillis,
                LocalTime.parse(ends[0].trim()), LocalTime.parse(ends[1].trim()), ZoneId.of(zone.trim()));
    }

    boolean open(Instant now) {
        LocalTime local = now.atZone(zone).toLocalTime();
        if (windowStart.isBefore(windowEnd)) {
            return !local.isBefore(windowStart) && local.isBefore(windowEnd);
        }
        return !local.isBefore(windowStart) || local.isBefore(windowEnd);
    }
}
