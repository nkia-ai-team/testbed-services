package com.corebanking.account.repository;

import com.corebanking.account.entity.Account;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface AccountRepository extends JpaRepository<Account, String> {

    @Query("select a from Account a where "
            + "(:status is null or a.status = :status) and "
            + "(:holder is null or lower(a.holder) like lower(concat('%', :holder, '%')))")
    Page<Account> search(@Param("status") String status, @Param("holder") String holder, Pageable pageable);

    List<Account> findByStatus(String status);
}
