package com.corebanking.ledger.repository;

import com.corebanking.ledger.entity.LedgerEntry;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.Collection;
import java.util.List;

public interface LedgerEntryRepository extends JpaRepository<LedgerEntry, Long> {
    List<LedgerEntry> findByTransferRef(String transferRef);

    /**
     * 대사용 불균형(출금 합 - 입금 합)을 한 SQL 문장으로 구한다. Oracle 은 문장 하나가
     * 한 읽기 시점(statement-level read consistency)을 보므로, 두 합계가 같은 순간의 원장을
     * 센다. 합계를 문장 두 개로 따로 구하면 그 사이에 커밋된 이체가 뒤 문장에만 잡혀
     * 장부가 맞는데도 불균형이 0 이 아니게 나온다(readOnly 트랜잭션이어도 Oracle 은
     * 문장마다 읽기 시점이 다르다). 문장을 둘로 나누지 않는다.
     */
    String IMBALANCE_SQL = "SELECT COALESCE(SUM(CASE WHEN direction = 'DEBIT' THEN amount ELSE 0 END), 0)"
            + " - COALESCE(SUM(CASE WHEN direction = 'CREDIT' THEN amount ELSE 0 END), 0)"
            + " FROM ledger_entries";

    @Query(value = IMBALANCE_SQL, nativeQuery = true)
    BigDecimal sumImbalance();

    @Query("select e from LedgerEntry e where "
            + "(:accountId is null or e.accountId = :accountId) and "
            + "(:transferRef is null or e.transferRef = :transferRef) and "
            + "(:direction is null or e.direction = :direction)")
    Page<LedgerEntry> search(@Param("accountId") String accountId,
                              @Param("transferRef") String transferRef,
                              @Param("direction") String direction,
                              Pageable pageable);

    /** 보존 기간 정리 대상 판정에 필요한 두 컬럼만 읽는다. */
    interface Head {
        String getTransferRef();

        LocalDateTime getCreatedAt();
    }

    // created_at 에는 인덱스가 없다. 조건으로 찾으면 매 주기 전체를 훑으므로, id 순 앞머리
    // 한 묶음만 읽고 기간 판정은 호출 쪽에서 한다(PK 만 탄다).
    @Query("select e.transferRef as transferRef, e.createdAt as createdAt from LedgerEntry e order by e.id asc")
    List<Head> findOldest(Pageable pageable);

    // 이체 참조번호 단위로 지운다 — 출금·입금 두 줄이 한 문장에서 함께 사라져야
    // 대사 배치(DEBIT 합 - CREDIT 합)가 거짓 불일치를 내지 않는다.
    @Modifying
    @Query("delete from LedgerEntry e where e.transferRef in :refs")
    int deleteByTransferRefIn(@Param("refs") Collection<String> refs);
}
