# social-feed testbed

NKIA RCA testbed - SNS 도메인 (게시물, 피드, 댓글, 알림) 마이크로서비스.

## 구조

- `post-service` (8080) — 게시물 작성/조회 + fan-out (feed_entries 적재)
- `feed-service` (8081) — 사용자 피드 조회
- `comment-service` (8082) — 댓글 작성/조회
- `notification-service` (8083) — 알림 발행 + 외부 push gateway 호출

## DB

- MySQL 8.0 (multiarch — ARM64 OK)
- 테이블: `posts`, `feed_entries`, `comments`, `notifications`

## failure_surfaces

| surface | trigger 위치 |
|---|---|
| lock-contention | post 작성 시 `feed_entries` 인덱스 락 (InnoDB row-lock) — `POST /api/posts` |
| external-timeout | notification → mock-push-gateway 호출 — `POST /api/notifications` |
| db-cpu-throttle | feed 조회 시 join + (옵션) LIKE 풀스캔 — `GET /api/feed/{userId}` |
| traffic-flood | viral 게시물 — `POST /api/posts` primary load |

## 로컬 dev

```bash
docker compose -f docker-compose.dev.yml up -d
./mvnw clean package -DskipTests
java -jar post-service/target/*.jar
```

## K3s 배포 (ARM64)

```bash
bash k8s/build-and-deploy.sh
```

배포 후 확인: `http://<host>:30081/api/posts`
