package com.corebanking.ledger.repository;

import com.corebanking.ledger.entity.LedgerEntry;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.math.BigDecimal;
import java.util.List;

public interface LedgerEntryRepository extends JpaRepository<LedgerEntry, Long> {
    List<LedgerEntry> findByTransferRef(String transferRef);

    @Query("select sum(e.amount) from LedgerEntry e where e.direction = :direction")
    BigDecimal sumAmountByDirection(@Param("direction") String direction);

    @Query("select e from LedgerEntry e where "
            + "(:accountId is null or e.accountId = :accountId) and "
            + "(:transferRef is null or e.transferRef = :transferRef) and "
            + "(:direction is null or e.direction = :direction)")
    Page<LedgerEntry> search(@Param("accountId") String accountId,
                              @Param("transferRef") String transferRef,
                              @Param("direction") String direction,
                              Pageable pageable);
}
