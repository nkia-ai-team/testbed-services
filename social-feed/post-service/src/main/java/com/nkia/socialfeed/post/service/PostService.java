package com.nkia.socialfeed.post.service;

import com.nkia.socialfeed.common.dto.PostRequest;
import com.nkia.socialfeed.common.dto.PostResponse;
import com.nkia.socialfeed.common.exception.ServiceException;
import com.nkia.socialfeed.post.entity.FeedEntry;
import com.nkia.socialfeed.post.entity.Post;
import com.nkia.socialfeed.post.repository.FeedEntryRepository;
import com.nkia.socialfeed.post.repository.PostRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

@Service
public class PostService {

    private static final Logger log = LoggerFactory.getLogger(PostService.class);

    private final PostRepository postRepository;
    private final FeedEntryRepository feedEntryRepository;

    public PostService(PostRepository postRepository, FeedEntryRepository feedEntryRepository) {
        this.postRepository = postRepository;
        this.feedEntryRepository = feedEntryRepository;
    }

    /**
     * 게시물 작성 + fan-out (post + feed_entries 동일 트랜잭션 내 행 락 발생 surface).
     * failure_surface: lock-contention. 동시 작성 시 feed_entries 인덱스 락 경합.
     */
    @Transactional
    public PostResponse createPost(PostRequest request) {
        Post post = new Post();
        post.setAuthorId(request.authorId());
        post.setContent(request.content());
        postRepository.save(post);

        // fan-out: 팔로워 N명 (단순화: author 자신 + 의사 팔로워 5명) 의 feed_entries 에 insert
        // 실제 운영에선 follower 서비스 조회 후 bulk insert. 여기선 self + fixed 5 user.
        long[] followerIds = new long[]{request.authorId(), 1L, 2L, 3L, 4L, 5L};
        for (long uid : followerIds) {
            FeedEntry entry = new FeedEntry();
            entry.setUserId(uid);
            entry.setPostId(post.getId());
            feedEntryRepository.save(entry);
        }

        log.info("Post created id={}, fan-out to {} users", post.getId(), followerIds.length);
        return toResponse(post);
    }

    @Transactional(readOnly = true)
    public PostResponse getPost(Long id) {
        Post post = postRepository.findById(id)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Post not found: " + id));
        return toResponse(post);
    }

    @Transactional(readOnly = true)
    public List<PostResponse> getPostsByAuthor(Long authorId) {
        return postRepository.findByAuthorIdOrderByIdDesc(authorId).stream()
                .map(this::toResponse)
                .toList();
    }

    private PostResponse toResponse(Post post) {
        return new PostResponse(
                post.getId(),
                post.getAuthorId(),
                post.getContent(),
                post.getCreatedAt()
        );
    }
}
