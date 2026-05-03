package com.nkia.socialfeed.post.repository;

import com.nkia.socialfeed.post.entity.FeedEntry;
import org.springframework.data.jpa.repository.JpaRepository;

public interface FeedEntryRepository extends JpaRepository<FeedEntry, Long> {
}
