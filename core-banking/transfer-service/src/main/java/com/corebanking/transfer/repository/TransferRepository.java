package com.corebanking.transfer.repository;

import com.corebanking.transfer.entity.Transfer;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

public interface TransferRepository extends JpaRepository<Transfer, Long> {
    Optional<Transfer> findByTransferRef(String transferRef);

    @Query("select t from Transfer t where "
            + "(:fromAccount is null or t.fromAccount = :fromAccount) and "
            + "(:toAccount is null or t.toAccount = :toAccount) and "
            + "(:status is null or t.status = :status) and "
            + "(:from is null or t.createdAt >= :from) and "
            + "(:to is null or t.createdAt <= :to)")
    Page<Transfer> search(@Param("fromAccount") String fromAccount,
                           @Param("toAccount") String toAccount,
                           @Param("status") String status,
                           @Param("from") LocalDateTime from,
                           @Param("to") LocalDateTime to,
                           Pageable pageable);

    // 네이티브 쿼리 사용 이유: JPQL function('trunc', ...) 는 Hibernate 가 등록한 표준 trunc(number)
    // 시그니처만 검증하므로 날짜 인자를 주면 "Parameter 1 ... has type NUMERIC" 로 거부된다(실기동 검증 중 발견).
    // Oracle TRUNC(date) 는 네이티브로 직접 호출.
    @Query(value = "select trunc(created_at) as day, count(*) as cnt, sum(amount) as total "
            + "from transfers where status = 'COMPLETED' and created_at >= :since "
            + "group by trunc(created_at) order by trunc(created_at) desc", nativeQuery = true)
    List<Object[]> dailyStatsSince(@Param("since") LocalDateTime since);

    List<Transfer> findByStatusAndCreatedAtBefore(String status, LocalDateTime before);
}
