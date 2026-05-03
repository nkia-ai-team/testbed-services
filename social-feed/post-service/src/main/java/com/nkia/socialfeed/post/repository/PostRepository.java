package com.nkia.socialfeed.post.repository;

import com.nkia.socialfeed.post.entity.Post;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface PostRepository extends JpaRepository<Post, Long> {
    List<Post> findByAuthorIdOrderByIdDesc(Long authorId);
}
