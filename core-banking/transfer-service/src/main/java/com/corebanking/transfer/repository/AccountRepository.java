package com.corebanking.transfer.repository;

import com.corebanking.transfer.entity.Account;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Optional;

public interface AccountRepository extends JpaRepository<Account, String> {

    /**
     * SELECT ... FOR UPDATE. 이체 트랜잭션에서 출금/입금 계좌 row 를 lock 한다.
     * 동시 이체 시 같은 계좌를 대상으로 row-lock 경합이 발생 → lock-contention 장애 표면.
     */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select a from Account a where a.id = :id")
    Optional<Account> findByIdForUpdate(@Param("id") String id);
}
