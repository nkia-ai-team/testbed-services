package com.nkia.socialfeed.feed.service;

import com.nkia.socialfeed.common.dto.FeedEntryResponse;
import com.nkia.socialfeed.feed.repository.FeedEntryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigInteger;
import java.sql.Timestamp;
import java.time.LocalDateTime;
import java.util.List;

@Service
public class FeedService {

    private static final Logger log = LoggerFactory.getLogger(FeedService.class);

    private final FeedEntryRepository feedEntryRepository;

    public FeedService(FeedEntryRepository feedEntryRepository) {
        this.feedEntryRepository = feedEntryRepository;
    }

    @Transactional(readOnly = true)
    public List<FeedEntryResponse> getFeed(Long userId) {
        log.debug("Fetching feed for user={}", userId);
        List<Object[]> rows = feedEntryRepository.findFeedRowsByUserId(userId);
        return rows.stream().map(this::mapRow).toList();
    }

    private FeedEntryResponse mapRow(Object[] row) {
        Long id = toLong(row[0]);
        Long userId = toLong(row[1]);
        Long postId = toLong(row[2]);
        String content = row[3] != null ? row[3].toString() : null;
        Long authorId = toLong(row[4]);
        LocalDateTime createdAt = toLocalDateTime(row[5]);
        return new FeedEntryResponse(id, userId, postId, content, authorId, createdAt);
    }

    private Long toLong(Object obj) {
        if (obj == null) return null;
        if (obj instanceof Long l) return l;
        if (obj instanceof Number n) return n.longValue();
        if (obj instanceof BigInteger bi) return bi.longValue();
        return Long.parseLong(obj.toString());
    }

    private LocalDateTime toLocalDateTime(Object obj) {
        if (obj == null) return null;
        if (obj instanceof LocalDateTime ldt) return ldt;
        if (obj instanceof Timestamp ts) return ts.toLocalDateTime();
        return null;
    }
}
